"""Versioned append-only trace, content-addressed blobs, and strict validation."""

import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 1
TRACKED = {"app.sqlite", "app.sqlite-journal", "app.sqlite-wal"}
EVENT_TYPES = {"open", "close", "write", "truncate", "sync", "delete", "begin", "ack", "marker"}
EFFECT_TYPES = {"create", "write", "truncate", "delete"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_file(name: str) -> str:
    """No paths from a trace may escape an isolated materialization directory."""
    if type(name) is not str or name not in TRACKED:
        raise ValueError(f"unsupported SQLite trace file: {name!r}")
    return name


class TraceWriter:
    def __init__(self, root: Path, target: Path):
        self.root = root.resolve()
        self.target = target
        self.blobs = target / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.stream = (target / "events.jsonl").open("w", encoding="utf-8")
        self.sequence = 0
        self.events = []
        self.generations = {}
        self.exists = {name: (root / name).exists() for name in TRACKED}

    def path(self, name):
        import apsw

        if name is None:
            return None
        raw = name.filename() if isinstance(name, apsw.URIFilename) else str(name)
        path = Path(raw).resolve()
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return None  # SQLite-owned temporary files outside the fixture
        return safe_file(rel)

    def append(self, kind: str, file=None, payload=None, **properties):
        if kind not in EVENT_TYPES:
            raise ValueError(f"unknown trace event: {kind}")
        event = {"seq": self.sequence, "kind": kind}
        if file is not None:
            event["file"] = safe_file(file)
            event["generation"] = self.generations.get(file, 0)
        if payload is not None:
            raw = bytes(payload)
            sha = digest(raw)
            blob = self.blobs / sha
            if not blob.exists():
                blob.write_bytes(raw)
            event.update(blob=sha, length=len(raw))
        event.update(properties)
        self.stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        self.stream.flush()
        self.events.append(event)
        self.sequence += 1
        return event

    def opened(self, file, existed, flags):
        if not existed:
            self.generations[file] = self.generations.get(file, -1) + 1
            self.exists[file] = True
        self.append("open", file, created=not existed, flags=int(flags))

    def deleted(self, file, syncdir):
        self.exists[file] = False
        self.append("delete", file, syncdir=bool(syncdir))

    def finish(self, metadata):
        self.stream.close()
        manifest = dict(schema_version=SCHEMA_VERSION, event_count=self.sequence, **metadata)
        dump_json(self.target / "manifest.json", manifest)


def read_trace(target: Path):
    """Reject malformed traces before any materialization or replay."""
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported trace schema version")
    events = []
    with (target / "events.jsonl").open(encoding="utf-8") as stream:
        for seq, line in enumerate(stream):
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("seq") != seq or event.get("kind") not in EVENT_TYPES:
                raise ValueError(f"invalid or out-of-order event {seq}")
            kind = event["kind"]
            if "file" in event:
                safe_file(event["file"])
                if type(event.get("generation")) is not int or event["generation"] < 0:
                    raise ValueError("invalid file generation")
            elif kind in ("open", "close", "write", "truncate", "sync", "delete"):
                raise ValueError(f"file event {seq} has no file")
            if kind == "write":
                if type(event.get("offset")) is not int or event["offset"] < 0:
                    raise ValueError("invalid write offset")
                if type(event.get("length")) is not int or event["length"] < 0:
                    raise ValueError("invalid write length")
                sha = event.get("blob")
                if type(sha) is not str or len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
                    raise ValueError("invalid blob reference")
                blob = target / "blobs" / sha
                if not blob.is_file() or blob.stat().st_size != event["length"] or digest(blob.read_bytes()) != sha:
                    raise ValueError("missing or corrupted write payload")
            if kind == "truncate" and (type(event.get("size")) is not int or event["size"] < 0):
                raise ValueError("invalid truncate size")
            if kind == "open" and (type(event.get("created")) is not bool):
                raise ValueError("invalid open/create event")
            if kind == "sync" and type(event.get("flags")) is not int:
                raise ValueError("invalid sync flags")
            if kind in ("begin", "ack") and (type(event.get("ordinal")) is not int or event["ordinal"] < 0):
                raise ValueError("invalid operation ordinal")
            events.append(event)
    if manifest.get("event_count") != len(events):
        raise ValueError("trace count does not match manifest")
    return manifest, events
