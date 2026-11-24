"""Public scenario API for CrashCheck."""
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Callable

__version__ = "0.2.0"


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
    """Per-worker handle given to scenario hooks.

    ``strict_ordering`` enforces the default oracle contract: an operation may begin
    only after the previous one was acknowledged, so at most one terminal operation is
    in flight at any cut. A scenario opts out with ``STRICT_ORDERING = False``.
    """
    CLOCK_EPOCH = 1_700_000_000.0

    def __init__(self, root: Path, seed: int, on_marker: Callable | None = None,
                 strict_ordering: bool = True):
        self.root = root
        self.db = root / "app.sqlite"
        self.seed = seed
        self.random = Random(seed)
        self.strict_ordering = strict_ordering
        self._on_marker = on_marker
        self._begun = []
        self._acknowledged = []
        self._ticks = 0
        self._ids = 0

    def connect(self, readonly: bool = False, foreign_keys: bool = True):
        """Connection factory. Use ``readonly=True`` inside ``observe``."""
        if readonly:
            db = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        else:
            db = sqlite3.connect(self.db)
        db.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        return db

    def now(self) -> float:
        """Deterministic clock: one simulated second per call, offset by the seed."""
        self._ticks += 1
        return self.CLOCK_EPOCH + self.seed + self._ticks

    def new_id(self, prefix: str = "id") -> str:
        """Deterministic identifier generator."""
        self._ids += 1
        return f"{prefix}-{self.seed}-{self._ids:06d}"

    @property
    def history(self):
        return History(tuple(self._begun), tuple(self._acknowledged))

    def begin(self, ordinal: int):
        if type(ordinal) is not int or ordinal < 0 or ordinal in self._begun:
            raise ValueError("begin requires a new non-negative integer ordinal")
        if self.strict_ordering and set(self._begun) - set(self._acknowledged):
            raise ValueError("begin requires the previous operation to be acknowledged "
                             "(set STRICT_ORDERING = False to allow overlap)")
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
