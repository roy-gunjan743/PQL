"""APSW pass-through VFS, with a narrow sqlite3-compatible workload bridge."""

from __future__ import annotations

import re
from pathlib import Path

import apsw

from .trace import TraceWriter


class TracingFile(apsw.VFSFile):
    def __init__(self, base, name, flags, writer: TraceWriter, file: str):
        super().__init__(base, name, flags)
        self.writer, self.file = writer, file

    def xWrite(self, data, offset):
        super().xWrite(data, offset)
        self.writer.append("write", self.file, payload=data, offset=int(offset))

    def xSync(self, flags):
        super().xSync(flags)
        self.writer.append("sync", self.file, flags=int(flags))

    def xTruncate(self, size):
        super().xTruncate(size)
        self.writer.append("truncate", self.file, size=int(size))

    def xClose(self):
        super().xClose()
        self.writer.append("close", self.file)


class TracingVFS(apsw.VFS):
    def __init__(self, writer: TraceWriter, name="crashcheck-record", base=None):
        self.writer = writer
        self.base = base or apsw.vfs_names()[0]
        self.name = name
        super().__init__(name, base=self.base)

    def xOpen(self, name, flags):
        file = self.writer.path(name)
        if file is None:
            return super().xOpen(name, flags)
        existed = (self.writer.root / file).exists()
        handle = TracingFile(self.base, name, flags, self.writer, file)
        self.writer.opened(file, existed, flags[0])
        return handle

    def xDelete(self, name, syncdir):
        file = self.writer.path(name)
        super().xDelete(name, syncdir)
        if file is not None:
            self.writer.deleted(file, syncdir)


class CompatibleCursor:
    """Preserve implicit DML transactions for a small sqlite3 API subset."""

    def __init__(self, conn):
        self.conn = conn
        self.raw = conn.raw.cursor()

    def execute(self, sql, parameters=()):
        self.conn._begin_if_needed(sql)
        self.raw.execute(sql, parameters)
        if re.match(r"^\s*PRAGMA\s+\w+\s*[=(]", sql, re.IGNORECASE):
            self.conn.assert_profile()
        return self

    def executemany(self, sql, rows):
        self.conn._begin_if_needed(sql)
        self.raw.executemany(sql, rows)
        return self

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()

    def __iter__(self):
        return iter(self.raw)

    @property
    def description(self):
        try:
            return self.raw.getdescription()
        except apsw.ExecutionCompleteError:
            return None


class CompatibleConnection:
    """Supports the sqlite3 connection operations needed by the reference scenarios.

    It deliberately refuses unsupported adapters/factories instead of pretending a
    Python sqlite3 application was traced when it actually bypassed APSW.
    """

    DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)

    def __init__(self, path, vfs, isolation_level="DEFERRED", timeout=5.0):
        self.raw = apsw.Connection(str(path), vfs=vfs)
        self.raw.setbusytimeout(round(float(timeout) * 1000))
        self.isolation_level = isolation_level
        self.closed = False
        mode = str(self.raw.pragma("journal_mode")).lower()
        if mode != "delete":
            self.raw.close()
            raise ValueError(f"file-prefix-v1 requires journal_mode=DELETE, found {mode}")
        self.raw.pragma("synchronous", "EXTRA")
        self.raw.pragma("mmap_size", 0)
        self.raw.pragma("temp_store", "MEMORY")
        if int(self.raw.pragma("synchronous")) != 3:
            raise ValueError("storage recorder requires synchronous=EXTRA")
        if self.raw.pragma("mmap_size") not in (0, None):
            raise ValueError("storage recorder requires mmap_size=0")

    def assert_profile(self):
        mode = str(self.raw.pragma("journal_mode")).lower()
        sync = int(self.raw.pragma("synchronous"))
        mmap = self.raw.pragma("mmap_size")
        if mode != "delete" or sync != 3 or mmap not in (0, None):
            raise ValueError("unsupported changed SQLite storage profile: "
                             f"journal_mode={mode}, synchronous={sync}, mmap_size={mmap}")

    def _begin_if_needed(self, sql):
        if self.isolation_level is not None and self.raw.getautocommit() and self.DML.match(sql):
            self.raw.execute("BEGIN DEFERRED")

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def cursor(self):
        return CompatibleCursor(self)

    def executescript(self, script):
        self.commit()
        self.raw.execute(script)
        return self.cursor()

    def commit(self):
        if not self.raw.getautocommit():
            self.raw.execute("COMMIT")

    def rollback(self):
        if not self.raw.getautocommit():
            self.raw.execute("ROLLBACK")

    @property
    def in_transaction(self):
        return not self.raw.getautocommit()

    def close(self):
        if not self.closed:
            try:
                self.assert_profile()
            finally:
                self.raw.close()
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        self.rollback() if kind else self.commit()
        return False


class ConnectionBridge:
    """Replace sqlite3.connect only in the short-lived isolated record worker."""

    def __init__(self, ctx, vfs_name):
        self.db = ctx.db.resolve()
        self.vfs_name = vfs_name
        self.connections = []
        self.original = None

    def connect(self, database, timeout=5.0, detect_types=0, isolation_level="DEFERRED",
                check_same_thread=True, factory=None, cached_statements=128, uri=False, **kwargs):
        from urllib.parse import urlsplit, parse_qs, unquote

        if uri:
            parts = urlsplit(str(database))
            path = Path(unquote(parts.path)).resolve()
            if parts.scheme != "file" or parse_qs(parts.query).get("mode") == ["ro"]:
                raise ValueError("recording requires a writable file SQLite connection")
        else:
            path = Path(database).resolve()
        if path != self.db:
            raise ValueError("file-prefix-v1 records only ctx.db (app.sqlite)")
        if factory is not None or detect_types != 0 or kwargs or not check_same_thread:
            raise ValueError("unsupported sqlite3 connection options for APSW recording bridge")
        conn = CompatibleConnection(path, self.vfs_name, isolation_level, timeout)
        self.connections.append(conn)
        return conn

    def __enter__(self):
        import sqlite3
        self.original = sqlite3.connect
        sqlite3.connect = self.connect
        return self

    def __exit__(self, kind, error, traceback):
        import sqlite3
        sqlite3.connect = self.original
        if any(not conn.closed for conn in self.connections):
            for conn in self.connections:
                conn.close()
            if not error:
                raise ValueError("scenario left an SQLite connection open during recording")
