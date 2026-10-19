"""A failed upgrade must restore the old service and its database."""
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    module = importlib.import_module('deploy')
    root = tmp_path / 'fork'
    state = root / '.runtime'
    state.mkdir(parents=True)
    old = state / 'releases/old'
    old.mkdir(parents=True)
    new = state / 'releases/new/source'
    new.mkdir(parents=True)
    (new / 'pyproject.toml').write_text('[project]\nversion="0.6.1+privacy.1"\n')
    (state / 'current').symlink_to(old, target_is_directory=True)
    status = state / 'deployment.json'
    status.write_text(json.dumps({'commit': 'old', 'pid': 111, 'command': ['python', 'old'],
                                 'version': '0.6.1', 'release': str(old)}))
    store = tmp_path / 'store.db'
    with sqlite3.connect(store) as db:
        db.executescript('CREATE TABLE mcp_servers (id TEXT, active INTEGER);'
            'CREATE TABLE rules (tool_name TEXT,field_path TEXT,alias_prefix TEXT,action TEXT,active INTEGER,target_name TEXT);'
            'INSERT INTO mcp_servers VALUES ("synthetic",1);'
            'INSERT INTO rules VALUES ("*","author","header","mask",1,"synthetic");')
    for name, value in [('ROOT', root), ('STATE', state), ('STATUS', status), ('STORE', store)]:
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, 'build_env', lambda: {})
    monkeypatch.setattr(module, 'git', lambda *args, **kwargs: '' if args[0] == 'status' else 'new')
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: 'python old\n')
    monkeypatch.setattr(module, 'original_environment', lambda pid: {})
    monkeypatch.setattr('sys.argv', ['deploy.py'])
    stopped = []
    monkeypatch.setattr(module, 'stop', stopped.append)
    launched = []

    def launch(command, env):
        launched.append(command)
        return SimpleNamespace(pid=222 if len(launched) == 1 else 333)

    monkeypatch.setattr(module, 'launch', launch)
    return module, state, store, old, stopped, launched


def test_failed_release_restores_database_and_previous_service(deployment, monkeypatch):
    module, state, store, old, stopped, launched = deployment
    checked = []

    def check(process, version, rules):
        checked.append(version)
        if len(checked) == 1:
            with sqlite3.connect(store) as db:
                db.execute('DELETE FROM rules')
            raise RuntimeError('Simulated failed migration')
        assert version == '0.6.1'
        assert len(rules['synthetic']) == 1

    monkeypatch.setattr(module, 'check_health', check)
    with pytest.raises(RuntimeError, match='previous service and database were restored'):
        module.main()
    assert (state / 'current').resolve() == old
    assert launched[-1] == ['python', 'old']
    assert stopped == [111, 222]
    assert json.loads((state / 'deployment.json').read_text())['pid'] == 333
    with sqlite3.connect(store) as db:
        assert db.execute('SELECT COUNT(*) FROM rules').fetchone()[0] == 1


def test_successful_release_retains_backup_and_records_selected_commit(deployment, monkeypatch):
    module, state, store, old, stopped, launched = deployment
    monkeypatch.setattr(module, 'check_health', lambda *args: None)
    module.main()
    result = json.loads((state / 'deployment.json').read_text())
    assert result['commit'] == 'new'
    assert result['previous_release'] == str(old)
    assert result['saved_rules_verified'] == 1
    assert (state / 'current').resolve() == state / 'releases/new'
    assert Path(result['database_backup']).is_file()
    assert stopped == [111]
