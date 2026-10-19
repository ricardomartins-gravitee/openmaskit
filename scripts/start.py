"""Start the selected immutable release and keep its PID record current."""
import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime'
settings_path = STATE / 'launch-settings.json'
if settings_path.exists():
    for key, value in json.loads(settings_path.read_text()).items():
        if key == 'OPENMASKIT_DISABLE_MARKETPLACE':
            os.environ.setdefault(key, value)
release = (STATE / 'current').resolve(strict=True)
executable = release / 'source/.venv/bin/python'
command = [str(executable), '-m', 'openmaskit', *sys.argv[1:]]
if any(arg in {'--version', '--help', '-h'} for arg in sys.argv[1:]):
    os.execv(str(executable), command)
try:
    with urllib.request.urlopen('http://127.0.0.1:9473/api/config', timeout=1) as response:
        if json.load(response).get('current_version'):
            raise SystemExit('A proxy is already running on these ports.')
except (OSError, ValueError):
    pass
status_path = STATE / 'deployment.json'
if status_path.exists():
    status = json.loads(status_path.read_text())
    status.update(pid=os.getpid(), command=command)
    temporary = status_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(status, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(status_path)
os.chdir(Path('~').expanduser())
os.execv(str(executable), command)
