"""Vendor a reviewed immutable design-system revision; rendering stays offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ricardomartins-gravitee/openmaskit-design-system"
FILES = {
    "web/tokens.css": "tokens.css",
    "web/components.css": "components.css",
    "web/tokens.resolved.json": "tokens.resolved.json",
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
    "TRADEMARKS.md": "TRADEMARKS.md",
}
DESTINATION = ROOT / "src/openmaskit/web/static/design"
LOCK = ROOT / "design-system.lock.json"


def validate_lock(lock):
    if lock.get("repository") != REPOSITORY or not re.fullmatch(
        r"[0-9a-f]{40}", lock.get("revision", "")
    ):
        raise ValueError(
            "Expected the design-system repository and an exact Git revision"
        )
    assets = lock.get("files", [])
    if not isinstance(assets, list) or any(
        not isinstance(asset, dict) or set(asset) != {"path", "sha256"}
        for asset in assets
    ):
        raise ValueError("Invalid design-system asset records")
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+", lock.get("version", ""))
        or set(asset["path"] for asset in assets) != set(FILES)
        or len(assets) != len(FILES)
    ):
        raise ValueError("Invalid design-system version or file list")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in (asset["sha256"] for asset in lock["files"])
    ):
        raise ValueError("Invalid design-system checksum")


def download(revision, name):
    # Callers validate revision and use the fixed file list before any request.
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{name}"
    with urlopen(url, timeout=30) as response:
        if not response.url.startswith("https://raw.githubusercontent.com/"):
            raise ValueError("Unexpected design-system download origin")
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Design asset exceeds size limit")
    return data


def verify(lock, contents):
    validate_lock(lock)
    if set(contents) != set(FILES):
        raise ValueError("Incomplete design asset set")
    for name, data in contents.items():
        if (
            hashlib.sha256(data).hexdigest()
            != {asset["path"]: asset["sha256"] for asset in lock["files"]}[name]
        ):
            raise ValueError(f"Design asset checksum mismatch: {name}")


def check(root=ROOT, upstream=False):
    lock = json.loads((root / "design-system.lock.json").read_text())
    validate_lock(lock)
    destination = root / "src/openmaskit/web/static/design"
    contents = {
        name: (destination / target).read_bytes() for name, target in FILES.items()
    }
    verify(lock, contents)
    if upstream:
        verify(lock, {name: download(lock["revision"], name) for name in FILES})
    return lock


def update(revision, root=ROOT):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(
            "Update requires an exact Git commit, never a moving branch/tag"
        )
    manifest = json.loads(download(revision, "design-manifest.json"))
    lock = {
        "repository": REPOSITORY,
        "revision": revision,
        "version": manifest["version"],
        "files": [asset for asset in manifest["files"] if asset["path"] in FILES],
    }
    validate_lock(lock)
    contents = {name: download(revision, name) for name in FILES}
    verify(lock, contents)  # Verify everything before changing any consumer file.
    destination = root / "src/openmaskit/web/static/design"
    destination.mkdir(parents=True, exist_ok=True)
    for name, target in FILES.items():
        path = destination / target
        if path.is_symlink():
            raise ValueError("Design assets cannot be symbolic links")
    for name, target in FILES.items():
        (destination / target).write_bytes(contents[name])
    (root / "design-system.lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n"
    )
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check", action="store_true", help="Verify vendored files offline (default)"
    )
    group.add_argument(
        "--check-upstream",
        action="store_true",
        help="Also compare with the pinned upstream revision",
    )
    group.add_argument(
        "--update",
        metavar="GIT_COMMIT",
        help="Explicitly update vendored assets and lock",
    )
    args = parser.parse_args()
    lock = update(args.update) if args.update else check(upstream=args.check_upstream)
    print(f"OpenMaskit design system {lock['version']} verified at {lock['revision']}")


if __name__ == "__main__":
    main()
