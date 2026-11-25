"""Storage-layer recording and crash-state modeling for CrashCheck.

The reference model is deliberately limited to a single SQLite database in
DELETE journal mode. Process-crash testing has no APSW dependency; the storage
adapter imports APSW only when modeled testing is requested.
"""

from .model import materialize, recipes, reductions
from .recorder import record

__all__ = ["record", "recipes", "materialize", "reductions"]
