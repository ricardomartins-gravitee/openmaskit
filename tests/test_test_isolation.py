"""Regression guard: running backend tests cannot create or reuse a personal key."""

from openmaskit.security import TokenEncryption


def test_default_encryption_key_stays_inside_this_tests_temporary_directory(tmp_path):
    key_path = TokenEncryption._KEY_PATH
    assert key_path.is_relative_to(tmp_path)
    assert not key_path.exists()
    encryption = TokenEncryption()
    encrypted = encryption.encrypt("synthetic-fixture-only")
    assert encryption.decrypt(encrypted) == "synthetic-fixture-only"
    assert key_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_each_test_starts_without_a_key_from_another_test(tmp_path):
    assert TokenEncryption._KEY_PATH.is_relative_to(tmp_path)
    assert not TokenEncryption._KEY_PATH.exists()


def test_a_tests_monkeypatch_undo_cannot_remove_the_key_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENMASKIT_SYNTHETIC_TEST_VALUE", "fixture")
    monkeypatch.undo()
    assert TokenEncryption._KEY_PATH.is_relative_to(tmp_path)
    assert TokenEncryption().decrypt(TokenEncryption().encrypt("fixture")) == "fixture"
