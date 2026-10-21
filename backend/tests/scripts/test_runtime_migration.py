"""离线验证会话库迁移：保留 WAL 中的数据、已有目标及运行锁。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_runtime import migrate_runtime


class RuntimeMigrationTests(unittest.TestCase):
    def test_backup_includes_wal_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "old.sqlite", root / "new" / "sessions.sqlite"
            db = sqlite3.connect(source)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE events (data TEXT)")
                db.execute("INSERT INTO events VALUES ('saved in WAL')")
                db.commit()
                self.assertTrue(migrate_runtime(source, target))
                with sqlite3.connect(target) as copied:
                    self.assertEqual(copied.execute("SELECT * FROM events").fetchall(),
                                     [("saved in WAL",)])
                self.assertEqual(db.execute("SELECT * FROM events").fetchall(),
                                 [("saved in WAL",)])
            finally:
                db.close()

    def test_existing_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            source.write_bytes(b"source must not be read when target exists")
            target = root / "target.sqlite"
            target.write_bytes(b"existing newer data")
            self.assertFalse(migrate_runtime(source, target))
            self.assertEqual(target.read_bytes(), b"existing newer data")

    def test_corrupt_source_does_not_publish_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "bad.sqlite", root / "new" / "sessions.sqlite"
            source.write_bytes(b"corrupt database")
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_runtime(source, target)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_active_backend_lock_refuses_migration(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sessions.sqlite"
            source.touch()
            with (root / "runtime.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "Stop the backend"):
                    migrate_runtime(source, root / "new" / "sessions.sqlite")
