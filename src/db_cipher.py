"""Открытие файлов SQLCipher. Сами таблицы здесь не создаются.

Кто вызывает: store, mail_store и audit_journal.
Почему отдельно: один и тот же способ закрыть файл нужен нескольким базам,
а схемы и ключи у них разные.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

Connection = sqlcipher.Connection

_HEADER = b"SQLite format 3\x00"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_umask_done = False


def db_secret() -> str:
    value = os.environ.get("STRAZH_DB_SECRET", "").strip()
    if len(value) < 16:
        raise RuntimeError(
            "Задайте STRAZH_DB_SECRET не короче 16 символов. "
            "База не подставляет секрет из кода."
        )
    return value


def connect(path: Path, *, secret: str | None = None) -> Connection:
    key = secret if secret is not None else db_secret()
    _owner_only()
    path.parent.mkdir(parents=True, exist_ok=True)
    _seal_if_plaintext(path, key)
    conn = sqlcipher.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlcipher.Row
    conn.execute(f"PRAGMA key = {_quote(key)}")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master")
    except sqlcipher.DatabaseError as exc:
        conn.close()
        raise RuntimeError("Файл базы не открылся этим ключом.") from exc
    restrict(path)
    return conn


def restrict(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.is_file():
            os.chmod(candidate, 0o600)


def _owner_only() -> None:
    global _umask_done
    if _umask_done:
        return
    os.umask(0o077)
    _umask_done = True


def _quote(secret: str) -> str:
    return "'" + secret.replace("'", "''") + "'"


def _needs_seal(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(16) == _HEADER
    except OSError:
        return False


def _seal_if_plaintext(path: Path, secret: str) -> None:
    if not _needs_seal(path):
        return
    lock_path = Path(str(path) + ".seal.lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _flock(fd, exclusive=True)
        if _needs_seal(path):
            _seal(path, secret)
    finally:
        _flock(fd, exclusive=False)
        os.close(fd)
    restrict(lock_path)


def _flock(fd: int, *, exclusive: bool) -> None:
    try:
        import fcntl
    except ImportError:
        return
    fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)


def _table_counts(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row[0])
        if not _IDENT.fullmatch(name):
            continue
        counts[name] = int(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
    return counts


def _seal(path: Path, secret: str) -> None:
    src = sqlite3.connect(str(path))
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        counts = _table_counts(src)
        dump = "\n".join(src.iterdump())
    finally:
        src.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.is_file():
            side.unlink()
    sealed = Path(str(path) + ".sealed")
    if sealed.exists():
        sealed.unlink()
    enc = sqlcipher.connect(str(sealed))
    try:
        enc.execute(f"PRAGMA key = {_quote(secret)}")
        enc.executescript(dump)
        enc.commit()
        if _table_counts(enc) != counts:
            raise RuntimeError(
                "Закрытие базы остановлено: число строк не совпало. Прежний файл на месте."
            )
    except Exception:
        enc.close()
        if sealed.exists():
            sealed.unlink()
        raise
    enc.close()
    os.replace(sealed, path)
    restrict(path)
