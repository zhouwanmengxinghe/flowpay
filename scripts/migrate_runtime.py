"""在后端停止后迁移旧会话库；源库保留，已有目标库绝不覆盖。"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def legacy_lock(source: Path) -> Generator[None]:
    """Linux 容器内检查旧运行锁，防止复制正在运行的后端数据目录。"""
    lock_path = source.parent / "runtime.lock"
    if not lock_path.exists():
        yield
        return
    import fcntl

    with lock_path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Stop the backend before migrating its runtime database") from error
        yield


def migrate_runtime(source: Path, target: Path) -> bool:
    """用 SQLite backup 复制完整快照（含 WAL），校验后发布；返回是否新建了目标库。"""
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError("Source and target must differ")
    if target.exists() or not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    with legacy_lock(source):
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".sqlite", delete=False) as file:
            temporary = Path(file.name)
        try:
            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
                closing(sqlite3.connect(temporary)) as copied,
            ):
                original.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise RuntimeError("Migrated SQLite database failed integrity check")
                expected = original.execute("SELECT COUNT(*) FROM events").fetchone()
                if copied.execute("SELECT COUNT(*) FROM events").fetchone() != expected:
                    raise RuntimeError("Migrated event count differs from source")
            # 同目录硬链接原子发布；并发出现目标库时也不会覆盖它。
            target.hardlink_to(temporary)
            return True
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    migrated = migrate_runtime(args.source, args.target)
    print(
        "Runtime database migrated; source preserved"
        if migrated
        else "Migration skipped: target exists or no legacy database"
    )


if __name__ == "__main__":
    main()
