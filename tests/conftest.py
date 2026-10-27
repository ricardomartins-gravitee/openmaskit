"""All in-process tests use disposable encryption keys, never the user's key."""

import pytest

from openmaskit.security import TokenEncryption


@pytest.fixture(autouse=True)
def isolated_encryption_key(tmp_path):
    # A test's monkeypatch.undo() must not remove the suite's isolation boundary.
    with pytest.MonkeyPatch.context() as isolated:
        isolated.delenv("OPENMASKIT_ENCRYPTION_KEY", raising=False)
        isolated.setattr(TokenEncryption, "_KEY_PATH", tmp_path / "private-key" / ".key")
        yield
