"""Merge upstream into an isolated candidate; publish only after validation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime'
GIT = os.environ.get('GIT_BIN') or shutil.which('git') or 'git'
if Path('/Library/Developer/CommandLineTools/usr/bin/git').exists():
    GIT = '/Library/Developer/CommandLineTools/usr/bin/git'
UV = os.environ.get('UV_BIN') or shutil.which('uv') or '/opt/homebrew/bin/uv'


def run(args, cwd=ROOT, capture=False, env=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
        text=True, stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None, check=True)
    return result.stdout.strip() if capture else ''


def git(*args, cwd=ROOT, capture=False):
    return run([GIT, *args], cwd=cwd, capture=capture)


def build_env():
    for name in ['tmp', 'cache', 'python']:
        (STATE / name).mkdir(parents=True, exist_ok=True)
    return {**os.environ, 'UV_CACHE_DIR': str(STATE / 'cache'),
            'UV_PYTHON_INSTALL_DIR': str(STATE / 'python'), 'TMPDIR': str(STATE / 'tmp')}


def validate(candidate, env):
    commands = [
        [UV, 'sync', '--frozen', '--python', '3.12'],
        [UV, 'run', '--frozen', 'pytest', 'tests/', '-q', '--ignore=tests/e2e', '--basetemp=.runtime/pytest-temp'],
        [UV, 'run', '--frozen', 'python', 'scripts/privacy_smoke.py'],
        [UV, 'build', '--wheel'],
    ]
    for index, command in enumerate(commands):
        log_path = STATE / f'candidate-check-{index}.log'
        with log_path.open('w') as log:
            result = subprocess.run(command, cwd=candidate, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            raise RuntimeError(f'Candidate check {index} failed; see {log_path}')


def maintain(args):
    if git('status', '--porcelain', capture=True):
        raise RuntimeError('The maintained checkout has uncommitted changes; update left untouched.')
    config = json.loads((ROOT / 'fork.json').read_text())
    git('fetch', '--no-tags', config['upstream'], config['upstream_branch'])
    upstream = git('rev-parse', 'FETCH_HEAD', capture=True)
    base = git('rev-parse', 'HEAD', capture=True)
    today = datetime.now(timezone.utc).date()
    last = datetime.fromisoformat(config['dependencies_refreshed']).date()
    refresh = args.dependencies or (today - last).days >= 7
    changed = upstream != config['upstream_commit']
    if not changed and not refresh:
        if args.deploy:
            deployed = json.loads((STATE / 'deployment.json').read_text()) if (STATE / 'deployment.json').exists() else {}
            if deployed.get('commit') != base:
                env = build_env()
                validate(ROOT, env)
                run([sys.executable, str(ROOT / 'scripts/deploy.py')], env=env)
                return {'status': 'deployed', 'upstream_commit': upstream, 'commit': base}
        return {'status': 'unchanged', 'upstream_commit': upstream, 'commit': base}

    candidate = STATE / 'candidates' / f'{base[:12]}-{upstream[:12]}-{today.isoformat()}'
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        # A previous failed candidate is retained for diagnosis and never reused.
        candidate = candidate.with_name(candidate.name + '-' + datetime.now(timezone.utc).strftime('%H%M%S'))
    git('clone', '--no-hardlinks', str(ROOT), str(candidate))
    git('config', 'user.name', 'MCP Privacy Proxy maintenance', cwd=candidate)
    git('config', 'user.email', 'maintenance@localhost', cwd=candidate)
    git('fetch', '--no-tags', config['upstream'], config['upstream_branch'], cwd=candidate)
    if changed:
        git('merge', '--no-edit', 'FETCH_HEAD', cwd=candidate)
    env = build_env()
    lock_before = (candidate / 'uv.lock').read_bytes()
    if refresh:
        run([UV, 'lock', '--upgrade', '--python', '3.12'], cwd=candidate, env=env)
    dependencies_changed = (candidate / 'uv.lock').read_bytes() != lock_before
    if not changed and not dependencies_changed:
        return {'status': 'unchanged', 'upstream_commit': upstream, 'commit': base,
                'dependencies_checked': today.isoformat()}

    upstream_project = tomllib.loads(git('show', 'FETCH_HEAD:pyproject.toml', cwd=candidate, capture=True))
    version = upstream_project['project']['version'].split('+')[0]
    suffix = f'privacy.{config["privacy_revision"]}.g{upstream[:10]}'
    if dependencies_changed:
        suffix += '.d' + today.strftime('%Y%m%d')
    project = candidate / 'pyproject.toml'
    text = project.read_text()
    assert 'name = "mcp-privacy-proxy"' in text, 'Fork package identity was lost'
    text = re.sub(r'^version = "[^"]+"', f'version = "{version}+{suffix}"', text, count=1, flags=re.M)
    project.write_text(text)
    config['upstream_commit'] = upstream
    if refresh:
        config['dependencies_refreshed'] = today.isoformat()
    (candidate / 'fork.json').write_text(json.dumps(config, indent=2) + '\n')
    run([UV, 'lock', '--python', '3.12'], cwd=candidate, env=env)
    validate(candidate, env)
    git('add', 'pyproject.toml', 'uv.lock', 'fork.json', cwd=candidate)
    git('commit', '-m', f'Maintain upstream {upstream[:10]} with validated privacy protections', cwd=candidate)
    tested = git('rev-parse', 'HEAD', cwd=candidate, capture=True)
    if git('rev-parse', 'HEAD', capture=True) != base or git('status', '--porcelain', capture=True):
        raise RuntimeError('Checkout changed while validation was running; deployment left untouched.')
    git('fetch', str(candidate), tested)
    git('merge', '--ff-only', 'FETCH_HEAD')
    result = {'status': 'updated', 'upstream_commit': upstream, 'commit': tested,
              'version': version + '+' + suffix, 'previous_commit': base}
    if args.push:
        remotes = git('remote', capture=True).splitlines()
        if 'origin' in remotes:
            git('push', 'origin', 'HEAD:main')
            result['published'] = True
        else:
            result['published'] = False
            result['publication_pending'] = 'GitHub remote has not been configured'
    if args.deploy:
        run([sys.executable, str(ROOT / 'scripts/deploy.py')], env=env)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--push', action='store_true')
    parser.add_argument('--deploy', action='store_true')
    parser.add_argument('--dependencies', action='store_true')
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    with (STATE / 'maintenance.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = maintain(args)
        except Exception as error:
            result = {'status': 'attention_required', 'error': str(error),
                      'live_service_changed': False}
            (STATE / 'maintenance-status.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))
            raise
        result['checked_at'] = datetime.now(timezone.utc).isoformat()
        (STATE / 'maintenance-status.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
