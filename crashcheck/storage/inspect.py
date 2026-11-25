"""Fast, read-only estimates for a recorded storage crash search.

The estimate counts legal prefix combinations at every crash boundary without
enumerating or materializing them. It intentionally includes duplicate raw
recipes; the backend's byte-level deduplication occurs later.
"""

from collections import Counter
from math import prod
from pathlib import Path

from .model import effect
from .trace import read_trace


def summarize_trace(path: Path) -> dict:
    """Describe a validated trace and bound the raw candidate search size.

    The count is an upper bound, not a count of distinct on-disk states: many
    cuts or prefix combinations can materialize to identical file contents.
    """
    manifest, events = read_trace(path)
    counts: dict[str, int] = {}
    floors: dict[str, int] = {}
    event_types: Counter[str] = Counter()
    cuts = 0
    raw_combinations = 0

    for event in events:
        kind = event["kind"]
        event_types[kind] += 1
        file = event.get("file")
        changed = effect(event)

        if changed:
            counts[file] = counts.get(file, 0) + 1
        if kind == "sync" or (kind == "delete" and event["syncdir"]):
            floors[file] = counts.get(file, 0)

        if changed or kind in {"sync", "begin", "ack", "marker"}:
            cuts += 1
            raw_combinations += prod(
                counts[name] - floors.get(name, 0) + 1 for name in counts
            )

    return {
        "model": manifest.get("model"),
        "schema_version": manifest["schema_version"],
        "event_count": len(events),
        "events_by_kind": dict(sorted(event_types.items())),
        "effect_counts": dict(sorted(counts.items())),
        "durability_floors": dict(sorted(floors.items())),
        "crash_boundaries": cuts,
        "candidate_upper_bound": raw_combinations + int(bool(events)),
        "includes_duplicate_recipes": True,
    }
