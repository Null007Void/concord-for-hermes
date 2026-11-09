"""Recovery refuses damaged state and verifies candidates before replacing files."""
import sqlite3

import pytest

import concord_backup as B
import concord_kernel as K


def _store(path, *, held=False):
    coord = K.Coordinator(str(path))
    if held:
        coord.admit(object_key="object", operation_id="held", fingerprint="fp",
                    base_revision=1, actor="test")
        coord.begin_dispatch("held")
        coord.mark_unknown("held", "response lost")
    return path


def _sql(path, statement):
    conn = sqlite3.connect(path)
    try:
        conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("damage", [
    "DROP INDEX ux_concord_active_object",
    "DELETE FROM concord_operations",
    "UPDATE concord_counters SET value = 0 WHERE name = 'seq'",
    "ALTER TABLE concord_operations DROP COLUMN fingerprint",
])
def test_damaged_source_is_never_a_verified_backup(tmp_path, damage):
    source = _store(tmp_path / "source.db", held=True)
    _sql(source, damage)
    target = tmp_path / "backup.db"
    target.write_bytes(b"preserve existing backup")
    before = target.read_bytes()
    assert not B.inspect_store(source)["is_concord_store"]
    with pytest.raises(B.BackupError):
        B.backup_store(source, target, overwrite=True)
    assert target.read_bytes() == before
    with pytest.raises(B.BackupError):
        B.restore_store(source, target, replace=True)
    assert target.read_bytes() == before


def test_restore_damaged_destination_requires_explicit_replacement(tmp_path):
    source = _store(tmp_path / "backup.db")
    target = _store(tmp_path / "live.db", held=True)
    _sql(target, "DROP TABLE concord_attempts")
    before = target.read_bytes()
    with pytest.raises(B.BackupError, match="not a verified empty"):
        B.restore_store(source, target)
    assert target.read_bytes() == before
    assert B.restore_store(source, target, replace=True)["operations"] == 0


@pytest.mark.parametrize("restore", [False, True])
def test_damage_between_preflight_and_copy_preserves_destination(tmp_path, monkeypatch, restore):
    source = _store(tmp_path / "source.db", held=True)
    target = _store(tmp_path / "target.db", held=True)
    before = target.read_bytes()
    original = B._copy

    def changed_source(src, dst):
        _sql(src, "DROP INDEX ux_concord_active_object")
        return original(src, dst)

    monkeypatch.setattr(B, "_copy", changed_source)
    with pytest.raises(B.BackupError, match="staged copy did not verify"):
        if restore:
            B.restore_store(source, target, replace=True)
        else:
            B.backup_store(source, target, overwrite=True)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.concord-tmp-*"))


@pytest.mark.parametrize("restore", [False, True])
def test_same_file_is_refused_without_modification(tmp_path, restore):
    source = _store(tmp_path / "source.db", held=True)
    before = source.read_bytes()
    with pytest.raises(B.BackupError, match="different files"):
        if restore:
            B.restore_store(source, source, replace=True)
        else:
            B.backup_store(source, source, overwrite=True)
    assert source.read_bytes() == before
