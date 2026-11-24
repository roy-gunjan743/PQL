"""Public scenario API for CrashCheck."""
from dataclasses import dataclass
from pathlib import Path
from random import Random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable


@dataclass(frozen=True)
class History:
    begun: tuple[int, ...] = ()
    acknowledged: tuple[int, ...] = ()

    @classmethod
    def decode(cls, value):
        if set(value) != {"begun", "acknowledged"}:
            raise ValueError("expected begun and acknowledged operation ordinals")
        lists = []
        for name in ("begun", "acknowledged"):
            items = value[name]
            if not isinstance(items, list) or any(type(i) is not int or i < 0 for i in items):
                raise ValueError(f"invalid {name} ordinals")
            if len(set(items)) != len(items):
                raise ValueError(f"duplicate {name} ordinal")
            lists.append(tuple(items))
        if not set(lists[1]).issubset(lists[0]):
            raise ValueError("an acknowledgement must follow begin")
        return cls(*lists)


class Context:
    def __init__(self, root: Path, seed: int, on_marker: Callable | None = None):
        self.root = root
        self.db = root / "app.sqlite"
        self.seed = seed
        self.random = Random(seed)
        self._on_marker = on_marker
        self._begun = []
        self._acknowledged = []
        self.allow_concurrent = False
        self._ticks = 0

    def connect(self, readonly=False):
        mode = "ro" if readonly else "rw"
        db = sqlite3.connect(self.db.as_uri() + f"?mode={mode}", uri=True)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def now(self):
        self._ticks += 1
        return datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=self.seed + self._ticks)

    def new_id(self):
        return f"{self.random.getrandbits(128):032x}"

    @property
    def history(self):
        return History(tuple(self._begun), tuple(self._acknowledged))

    def begin(self, ordinal: int):
        if type(ordinal) is not int or ordinal < 0 or ordinal in self._begun:
            raise ValueError("begin requires a new non-negative integer ordinal")
        if not self.allow_concurrent and len(self._begun) != len(self._acknowledged):
            raise ValueError("previous operation must be acknowledged before the next begins")
        self._begun.append(ordinal)

    def acknowledge(self, ordinal: int):
        if type(ordinal) is not int or ordinal not in self._begun or ordinal in self._acknowledged:
            raise ValueError("acknowledge requires an outstanding operation")
        self._acknowledged.append(ordinal)

    def marker(self, name: str):
        if not isinstance(name, str) or not name:
            raise ValueError("marker requires a nonempty name")
        if self._on_marker:
            self._on_marker(name, self.history)


class CheckFailure(Exception):
    """Raise from observe to identify a verified application failure."""
    def __init__(self, check_id: str, failure_class: str, message: str):
        if not all(isinstance(x, str) and x.strip() for x in (check_id, failure_class, message)):
            raise ValueError("checks require an ID, failure class and message")
        super().__init__(message)
        self.check_id = check_id
        self.failure_class = failure_class
