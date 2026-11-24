"""Canonical JSON, raw-state hashes and outcome records."""
import hashlib
import json
import os
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                              indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def inventory(root: Path):
    """Include relative names, empty directories, lengths and file contents."""
    entries = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError(f"fixture contains a link: {name}")
        if path.is_dir():
            entries.append([name, "dir"])
        elif path.is_file():
            entries.append([name, "file", path.stat().st_size, file_hash(path)])
        else:
            raise ValueError(f"unsupported fixture entry: {name}")
    return entries


def fingerprint(entries):
    return hashlib.sha256(canonical(entries)).hexdigest()


def failure(check_id, failure_class, message):
    return {"status": "verified-failure", "check_id": check_id,
            "failure_class": failure_class, "detail": message}


def inconclusive(detail):
    return {"status": "inconclusive", "detail": detail}
