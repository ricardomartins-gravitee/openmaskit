"""Build an immutable release and switch the local service with rollback."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tarfile
import time
import tomllib
import urllib.parse
import urllib.request

from maintain import ROOT, STATE, GIT, UV, build_env, git

STORE = Path('~/.openmaskit/store.db').expanduser()
STATUS = STATE / 'deployment.json'


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def switch(release):
    temporary = STATE / 'current.next'
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(release, target_is_directory=True)
    temporary.replace(STATE / 'current')


def stop(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # A child owned by this updater can be reaped; unrelated PIDs are not waited on.
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
        except ChildProcessError:
            pass
        time.sleep(0.2)
    raise RuntimeError('Graceful shutdown timed out; no forced termination was performed')


def original_environment(pid):
    raw = subprocess.check_output([ 'ps', 'eww', '-p', str(pid), '-o', 'command=' ], text=True)
    matches = list(re.finditer(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=', raw))
    env = dict(os.environ)
    for index, match in enumerate(matches):
        key = match.group(1)
        if key.startswith(('OPENMASKIT_', 'MASKIT_', 'THUNDERBIRD_MCP_')) or key in {
            'PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE',
        }:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            env[key] = raw[match.end():end].rstrip()
    return env


def saved_rules():
    with sqlite3.connect(STORE.as_uri() + '?mode=ro', uri=True) as db:
        targets = [r[0] for r in db.execute('SELECT id FROM mcp_servers WHERE active=1')]
        return {target: set(db.execute(
            'SELECT tool_name, field_path, alias_prefix, action, active FROM rules WHERE target_name=?', (target,)
        ).fetchall()) for target in targets}


def check_health(process, version, expected_rules):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('New release exited during startup; inspect the local service log')
        try:
            with urllib.request.urlopen('http://127.0.0.1:9473/api/config', timeout=1) as response:
                if json.load(response)['current_version'] != version:
                    raise ValueError('Unexpected service version')
            for target, expected in expected_rules.items():
                with urllib.request.urlopen('http://127.0.0.1:9473/api/targets/' + urllib.parse.quote(target, safe='') + '/rules', timeout=1) as response:
                    actual = {(r['tool_name'], r['field_path'], r['alias_prefix'], r['action'], int(r['active']))
                              for r in json.load(response)['rules']}
                    if not expected.issubset(actual):
                        raise ValueError('Saved rules were not restored')
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError('New release did not restore the expected version and masking rules')


def launch(command, env):
    logfile = STATE / 'service.log'
    descriptor = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'ab', buffering=0) as log:
        return subprocess.Popen(command, cwd=str(Path('~').expanduser()), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adopt-pid', type=int)
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    if git('status', '--porcelain', capture=True):
        raise RuntimeError('Refusing deployment from an uncommitted source tree')
    commit = git('rev-parse', 'HEAD', capture=True)
    current = json.loads(STATUS.read_text()) if STATUS.exists() else None
    if current and current['commit'] == commit and not args.stage_only:
        with urllib.request.urlopen('http://127.0.0.1:9473/api/config', timeout=2) as response:
            if json.load(response)['current_version'] != current['version']:
                raise RuntimeError('The recorded release is not the running service')
        print(json.dumps({'status': 'already_deployed', 'commit': commit}))
        return
    release = STATE / 'releases' / commit
    source = release / 'source'
    if not source.exists():
        source.mkdir(parents=True)
        archive = subprocess.check_output([GIT, 'archive', commit], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter='data')
    environment = build_env()
    subprocess.run([UV, 'sync', '--frozen', '--no-dev', '--no-editable', '--python', '3.12', '--project', str(source)],
                   env=environment, check=True)
    version = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
    executable = source / '.venv/bin/python'
    command = [str(executable), '-m', 'openmaskit']
    if args.stage_only:
        print(json.dumps({'status': 'staged', 'commit': commit, 'version': version, 'release': str(release)}))
        return

    old_pid = args.adopt_pid if args.adopt_pid else current['pid'] if current else None
    if not old_pid:
        raise RuntimeError('Initial migration requires the PID of the existing service')
    old_command_text = subprocess.check_output(['ps', '-p', str(old_pid), '-o', 'command='], text=True).strip()
    if args.adopt_pid:
        old_command = [str(Path('~/.local/share/uv/tools/openmaskit/bin/python').expanduser()),
                       str(Path('~/.local/bin/openmaskit').expanduser())]
    else:
        old_command = current['command']
    if old_command_text != ' '.join(old_command):
        raise RuntimeError('Service PID does not match the recorded command; no process was stopped')
    env = original_environment(old_pid)
    expected_rules = saved_rules()
    backups = STATE / 'backups'
    backups.mkdir(exist_ok=True)
    backup = backups / ('store-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.db')
    stop(old_pid)
    process = None
    backup_complete = False
    try:
        with sqlite3.connect(STORE.as_uri() + '?mode=ro', uri=True) as original, sqlite3.connect(backup) as saved:
            original.backup(saved)
        backup.chmod(0o600)
        backup_complete = True
        switch(release)
        process = launch(command, env)
        check_health(process, version, expected_rules)
    except Exception:
        if process is not None:
            stop(process.pid)
        if backup_complete:
            with sqlite3.connect(backup) as saved, sqlite3.connect(STORE) as original:
                saved.backup(original)
        if current:
            switch(Path(current['release']))
        recovered = launch(old_command, env)
        old_version = current['version'] if current else '0.6.1'
        check_health(recovered, old_version, expected_rules)
        if current:
            current['pid'] = recovered.pid
            atomic_json(STATUS, current)
        raise RuntimeError('New release failed validation; previous service and database were restored')
    status = {'status': 'deployed', 'commit': commit, 'version': version, 'pid': process.pid,
              'release': str(release), 'command': command,
              'previous_release': current['release'] if current else None,
              'previous_command': old_command, 'database_backup': str(backup),
              'saved_rules_verified': sum(len(r) for r in expected_rules.values()),
              'deployed_at': datetime.now(timezone.utc).isoformat()}
    atomic_json(STATUS, status)
    # Retain the existing non-secret opt-out preference across manual starts.
    if 'OPENMASKIT_DISABLE_MARKETPLACE' in env:
        atomic_json(STATE / 'launch-settings.json', {
            'OPENMASKIT_DISABLE_MARKETPLACE': env['OPENMASKIT_DISABLE_MARKETPLACE']})
    print(json.dumps(status, indent=2))


if __name__ == '__main__':
    main()
