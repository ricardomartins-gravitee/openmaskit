"""The installed command must work when symlinked into the user's PATH."""
from pathlib import Path
import shutil
import subprocess


def test_launcher_resolves_its_source_through_an_installed_symlink(tmp_path):
    root = tmp_path / 'fork'
    (root / 'bin').mkdir(parents=True)
    (root / '.venv/bin').mkdir(parents=True)
    (root / 'scripts').mkdir()
    launcher = root / 'bin/mcp-privacy-proxy'
    shutil.copy2(Path(__file__).resolve().parents[1] / 'bin/mcp-privacy-proxy', launcher)
    python = root / '.venv/bin/python'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    installed = tmp_path / 'installed'
    installed.mkdir()
    command = installed / 'mcp-privacy-proxy'
    command.symlink_to(launcher)
    result = subprocess.run([str(command), '--version'], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [str(root / 'scripts/start.py'), '--version']
