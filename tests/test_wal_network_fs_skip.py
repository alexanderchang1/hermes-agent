"""Tests for the proactive WAL-skip on network / FUSE filesystems.

WAL's shared-memory coordination + fcntl byte-range locks are unreliable on
NFS/SMB/CIFS/FUSE (SQLite raises SQLITE_PROTOCOL "locking protocol"). Arming WAL
on a node where NFS locking transiently succeeds persists ``write_ver=2`` to
disk; every later open on a lock-failing node then fails — and the file can't be
auto-downgraded, because converting out of WAL also needs the failing locks.

``apply_wal_with_fallback`` therefore never *attempts* WAL when the DB file is
detected on a hostile filesystem — it stays in rollback-journal (DELETE) mode.

Note: on some HPC/CI runners the pytest tmpdir is itself NFS-backed, so the
"real detection" tests below are written to be environment-agnostic (they assert
the detector agrees with the actual statfs magic, not a hardcoded verdict).
"""

import sqlite3

import pytest

import hermes_state
from hermes_state import apply_wal_with_fallback


def _make_wal_blocking_conn(path):
    """Connection whose ``PRAGMA journal_mode=WAL`` raises, with an attempt counter."""
    attempts = [0]

    class _Conn(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                attempts[0] += 1
                raise sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    return sqlite3.connect(str(path), factory=_Conn, isolation_level=None), attempts


@pytest.fixture(autouse=True)
def _reset_dedup():
    hermes_state._wal_skip_warned_paths.clear()
    yield
    hermes_state._wal_skip_warned_paths.clear()


class TestNetworkFilesystemSkipsWal:
    def test_skips_wal_when_fs_is_hostile(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        conn, attempts = _make_wal_blocking_conn(tmp_path / "nfs.db")
        with caplog.at_level("INFO", logger="hermes_state"):
            mode = apply_wal_with_fallback(conn, db_label="netfs.db")
        assert mode == "delete"
        assert attempts[0] == 0, "WAL set-pragma must never be attempted on a hostile FS"
        infos = [r for r in caplog.records if "network/FUSE" in r.getMessage()]
        assert len(infos) == 1
        assert "netfs.db" in infos[0].getMessage()
        # DB still works for real writes.
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert list(conn.execute("SELECT x FROM t"))[0][0] == 1
        conn.close()

    def test_skip_info_is_deduped_per_label(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        with caplog.at_level("INFO", logger="hermes_state"):
            for _ in range(3):
                conn, _a = _make_wal_blocking_conn(tmp_path / "nfs.db")
                apply_wal_with_fallback(conn, db_label="dup.db")
                conn.close()
        infos = [r for r in caplog.records if "network/FUSE" in r.getMessage()]
        assert len(infos) == 1

    def test_attempts_wal_on_local_fs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: False)
        conn = sqlite3.connect(str(tmp_path / "ok.db"), isolation_level=None)
        assert apply_wal_with_fallback(conn) == "wal"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()

    def test_already_delete_db_on_hostile_fs_stays_delete_no_wal_attempt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        # Fresh DB defaults to DELETE journal mode.
        conn, attempts = _make_wal_blocking_conn(tmp_path / "already-delete.db")
        assert apply_wal_with_fallback(conn) == "delete"
        assert attempts[0] == 0
        conn.close()

    def test_probe_failure_on_hostile_fs_does_not_arm_wal(self, tmp_path, monkeypatch):
        """Gap-closure regression: even if the read-only probe itself raises
        ``locking protocol`` (flaky NFS), a hostile FS must NEVER attempt WAL —
        the arming window that persisted write_ver=2 despite the patch."""
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        p = tmp_path / "flaky.db"
        sqlite3.connect(str(p)).close()  # fresh DELETE-mode DB
        armed = [0]

        class _Conn(sqlite3.Connection):
            def execute(self, sql, *a, **k):  # type: ignore[override]
                s = sql.lower().replace(" ", "")
                if s == "pragmajournal_mode":
                    raise sqlite3.OperationalError("locking protocol")  # probe fails
                if "journal_mode=wal" in s:
                    armed[0] += 1
                    raise sqlite3.OperationalError("WAL must never be attempted")
                return super().execute(sql, *a, **k)

        conn = sqlite3.connect(str(p), factory=_Conn, isolation_level=None)
        mode = apply_wal_with_fallback(conn, db_label="flaky")
        conn.close()
        assert armed[0] == 0, "WAL was attempted despite hostile FS + probe failure"
        assert mode == "delete"

    def test_already_wal_on_hostile_fs_broken_locks_raises(self, tmp_path, monkeypatch):
        """Already-WAL DB on a hostile FS where locking is fully broken (probe,
        checkpoint, and downgrade all raise): can't open or convert — re-raise
        so the caller falls back (JSONL)."""
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        p = tmp_path / "wal-broken.db"
        prim = sqlite3.connect(str(p), isolation_level=None)
        prim.execute("PRAGMA journal_mode=WAL")
        prim.execute("CREATE TABLE t(x)")
        prim.close()
        assert hermes_state._on_disk_write_version(str(p)) == 2

        class _Conn(sqlite3.Connection):
            def execute(self, sql, *a, **k):  # type: ignore[override]
                s = sql.lower().replace(" ", "")
                if s.startswith("pragmajournal_mode") or "wal_checkpoint" in s:
                    raise sqlite3.OperationalError("locking protocol")
                return super().execute(sql, *a, **k)

        conn = sqlite3.connect(str(p), factory=_Conn, isolation_level=None)
        with pytest.raises(sqlite3.OperationalError, match="locking protocol"):
            apply_wal_with_fallback(conn, db_label="waltest")
        conn.close()

    def test_already_wal_on_hostile_fs_self_heals_to_delete(self, tmp_path, monkeypatch):
        """Self-healing: an already-WAL DB on a hostile FS where locking works is
        *converted* to DELETE (not preserved as WAL), on disk too."""
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        p = tmp_path / "wal-heal.db"
        prim = sqlite3.connect(str(p), isolation_level=None)
        prim.execute("PRAGMA journal_mode=WAL")
        prim.execute("CREATE TABLE t(x)")
        prim.close()
        assert hermes_state._on_disk_write_version(str(p)) == 2
        conn = sqlite3.connect(str(p), isolation_level=None)
        assert apply_wal_with_fallback(conn, db_label="heal") == "delete"
        conn.close()
        assert hermes_state._on_disk_write_version(str(p)) == 1  # rewritten on disk

    def test_shared_wal_on_hostile_fs_stays_wal(self, tmp_path, monkeypatch):
        """If another live connection holds WAL, SQLite refuses to downgrade
        (journal_mode=DELETE returns 'wal'); we keep using it as WAL."""
        monkeypatch.setattr(hermes_state, "_is_wal_hostile_filesystem", lambda p: True)
        p = tmp_path / "wal-shared.db"
        prim = sqlite3.connect(str(p), isolation_level=None)
        prim.execute("PRAGMA journal_mode=WAL")
        prim.execute("CREATE TABLE t(x)")
        prim.close()

        class _Conn(sqlite3.Connection):
            def execute(self, sql, *a, **k):  # type: ignore[override]
                s = sql.lower().replace(" ", "")
                if s == "pragmajournal_mode=delete":
                    # Simulate a concurrent WAL holder: downgrade refused, mode
                    # stays "wal".
                    return super().execute("PRAGMA journal_mode")
                return super().execute(sql, *a, **k)

        conn = sqlite3.connect(str(p), factory=_Conn, isolation_level=None)
        assert apply_wal_with_fallback(conn, db_label="shared") == "wal"
        conn.close()


class TestRealFilesystemDetection:
    def test_detection_agrees_with_real_magic(self, tmp_path):
        """The verdict must match the actual statfs magic of the path — works
        whether the test tmpdir is local (ext/xfs/tmpfs) or NFS-backed."""
        magic = hermes_state._filesystem_magic(str(tmp_path))
        expected = magic is not None and (
            magic in hermes_state._WAL_HOSTILE_FS_MAGIC
            or (magic & 0xFFFFFFFF) in hermes_state._WAL_HOSTILE_FS_MAGIC
        )
        assert hermes_state._is_wal_hostile_filesystem(str(tmp_path)) is expected

    def test_missing_path_uses_parent_dir(self, tmp_path):
        # A not-yet-created DB file resolves to its (existing) parent dir, so the
        # verdict matches the parent's.
        missing = tmp_path / "does-not-exist-yet.db"
        assert hermes_state._is_wal_hostile_filesystem(str(missing)) == \
            hermes_state._is_wal_hostile_filesystem(str(tmp_path))

    def test_magic_is_none_treated_as_not_hostile(self, monkeypatch):
        monkeypatch.setattr(hermes_state, "_filesystem_magic", lambda p: None)
        assert hermes_state._is_wal_hostile_filesystem("/whatever") is False

    def test_nfs_magic_is_hostile(self, monkeypatch):
        monkeypatch.setattr(hermes_state, "_filesystem_magic", lambda p: 0x6969)
        assert hermes_state._is_wal_hostile_filesystem("/nfs/path") is True

    def test_ext4_magic_is_not_hostile(self, monkeypatch):
        monkeypatch.setattr(hermes_state, "_filesystem_magic", lambda p: 0xEF53)
        assert hermes_state._is_wal_hostile_filesystem("/local/path") is False
