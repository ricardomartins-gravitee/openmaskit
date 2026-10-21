"""Offline integrity, updater rejection and actual served asset coverage."""

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from openmaskit.proxy.core import ProxyState
from openmaskit.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "design_system", ROOT / "scripts/design_system.py"
)
design = importlib.util.module_from_spec(spec)
spec.loader.exec_module(design)


def fixture_lock():
    lock = json.loads((ROOT / "design-system.lock.json").read_text())
    contents = {
        name: (design.DESTINATION / target).read_bytes()
        for name, target in design.FILES.items()
    }
    return lock, contents


def test_vendored_design_is_intact_and_imported_before_application_styles():
    lock = design.check()
    assert lock["version"] == "0.1.0"
    css = (ROOT / "src/openmaskit/web/static/style.css").read_text()
    assert css.index('@import url("design/tokens.css")') < css.index(
        "/* --- Reset --- */"
    )
    assert '@import url("design/components.css")' in css
    assert "--bg-root:" not in css  # Shared token values are not maintained twice.
    panel = (ROOT / "src/openmaskit/web/static/privacy_panel.html").read_text()
    assert 'class="om-button om-button-primary"' in panel
    assert 'class="om-button om-button-danger"' in panel


@pytest.mark.parametrize(
    "change", ["repository", "revision", "version", "files", "checksum"]
)
def test_invalid_lock_fails_before_network_access(change):
    lock, _ = fixture_lock()
    if change == "repository":
        lock[change] = "untrusted/elsewhere"
    if change == "revision":
        lock[change] = "main"
    if change == "version":
        lock[change] = "invalid"
    if change == "files":
        lock[change].append({"path": "../escape", "sha256": "0" * 64})
    if change == "checksum":
        lock["files"][0]["sha256"] = "invalid"
    with pytest.raises(ValueError):
        design.validate_lock(lock)


def test_tampered_or_incomplete_assets_fail():
    lock, contents = fixture_lock()
    contents["web/tokens.css"] += b"/* tampered */"
    with pytest.raises(ValueError, match="checksum mismatch"):
        design.verify(lock, contents)
    contents.pop("LICENSE")
    with pytest.raises(ValueError, match="Incomplete"):
        design.verify(lock, contents)


def test_update_verifies_all_files_before_writing(tmp_path):
    lock, contents = fixture_lock()
    manifest = {"version": lock["version"], "files": lock["files"]}
    data = {**contents, "design-manifest.json": json.dumps(manifest).encode()}
    with patch.object(
        design, "download", side_effect=lambda revision, name: data[name]
    ):
        assert design.update(lock["revision"], tmp_path) == lock
    assert design.check(tmp_path) == lock
    original = (tmp_path / "design-system.lock.json").read_bytes()
    data["web/tokens.css"] = b"tampered"
    with (
        patch.object(design, "download", side_effect=lambda revision, name: data[name]),
        pytest.raises(ValueError),
    ):
        design.update(lock["revision"], tmp_path)
    assert (tmp_path / "design-system.lock.json").read_bytes() == original
    assert design.check(tmp_path) == lock


def test_upstream_check_uses_pinned_revision_and_detects_drift():
    lock, contents = fixture_lock()

    def fetch(revision, name):
        assert revision == lock["revision"]
        return contents[name]

    with patch.object(design, "download", side_effect=fetch):
        assert design.check(upstream=True) == lock
    contents["web/components.css"] = b"tampered"
    with patch.object(design, "download", side_effect=fetch), pytest.raises(ValueError):
        design.check(upstream=True)


@pytest.mark.parametrize("revision", ["main", "v0.1.0", "../other", "a" * 39, "a" * 41])
def test_update_rejects_moving_or_invalid_revision(revision):
    with patch.object(design, "download") as fetch, pytest.raises(ValueError):
        design.update(revision)
    fetch.assert_not_called()


def test_update_rejects_symlinks(tmp_path):
    lock, contents = fixture_lock()
    manifest = {"version": lock["version"], "files": lock["files"]}
    destination = tmp_path / "src/openmaskit/web/static/design"
    destination.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (destination / "tokens.css").symlink_to(outside)
    data = {**contents, "design-manifest.json": json.dumps(manifest).encode()}
    with (
        patch.object(design, "download", side_effect=lambda revision, name: data[name]),
        pytest.raises(ValueError, match="symbolic"),
    ):
        design.update(lock["revision"], tmp_path)
    assert outside.read_text() == "unchanged"


@pytest.mark.asyncio
async def test_design_assets_are_served_by_the_real_application():
    lock, _ = fixture_lock()
    app = create_app(ProxyState(), csrf_token="synthetic-csrf")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:9473"
    ) as client:
        for name, target in design.FILES.items():
            response = await client.get("/static/design/" + target)
            assert response.status_code == 200
            assert (
                hashlib.sha256(response.content).hexdigest()
                == {asset["path"]: asset["sha256"] for asset in lock["files"]}[name]
            )
        for path in ["/", "/privacy", "/marketplace"]:
            response = await client.get(path)
            assert response.status_code == 200
            assert "OpenMaskit" in response.text
            assert "/static/style.css" in response.text


@pytest.mark.parametrize("change", [None, "asset", "imports"])
def test_built_wheel_assets_are_verified(tmp_path, change):
    lock, contents = fixture_lock()
    wheel_path = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, target in design.FILES.items():
            wheel.writestr(
                "openmaskit/web/static/design/" + target,
                b"tampered"
                if change == "asset" and name == "web/tokens.css"
                else contents[name],
            )
        css = (ROOT / "src/openmaskit/web/static/style.css").read_bytes()
        wheel.writestr(
            "openmaskit/web/static/style.css",
            b"body {}" if change == "imports" else css,
        )
    if change:
        with pytest.raises(ValueError):
            design.check_wheel(wheel_path)
    else:
        assert design.check_wheel(wheel_path) == lock
