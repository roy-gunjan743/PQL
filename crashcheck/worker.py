"""Internal subprocess entry point. All scenario code runs here."""
import hashlib
import importlib.util
import itertools
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from . import CheckFailure, Context, History
from .evidence import canonical, failure, inconclusive, read_json, write_json

CUT_EXIT = 77


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify(scenario, ctx, history):
    # mode=rw allows SQLite journal recovery without creating a missing database.
    try:
        db = sqlite3.connect(ctx.db.as_uri() + "?mode=rw", uri=True)
        try:
            rows = [r[0] for r in db.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                return failure("sqlite.integrity", "corruption", "\n".join(rows))
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return failure("sqlite.integrity", "corruption", str(exc))
        raise

    if hasattr(scenario, "recover"):
        scenario.recover(ctx)
    try:
        result = scenario.observe(ctx, history)
    except CheckFailure as exc:
        return failure(exc.check_id, exc.failure_class, str(exc))
    if result is not None:
        raise ValueError("observe must return None or raise CheckFailure")
    return {"status": "pass"}


def execute(job, reply):
    scenario = load_module(job["scenario"], "crashcheck_scenario")
    root = Path(job["root"])
    ctx = Context(root, job["seed"])
    action = job["action"]
    if action == "setup":
        scenario.setup(ctx)
        if not ctx.db.is_file():
            raise ValueError("setup must create app.sqlite")
        return {}
    if action == "verify":
        return verify(scenario, ctx, History.decode(job["history"]))
    if action in ("discover", "cut"):
        plan = []
        prefix = hashlib.sha256()
        count = 0

        def marker(name, history):
            nonlocal count
            event = {"index": count, "name": name, "history": asdict(history)}
            prefix.update(canonical(event) + b"\n")
            event["prefix"] = prefix.hexdigest()
            if action == "cut" and count == job["cut"]["index"]:
                if canonical(event) != canonical(job["cut"]):
                    raise ValueError("scenario marker sequence diverged from discovery")
                write_json(reply, {"value": event})
                os._exit(CUT_EXIT)
            if action == "discover" and count < job["limit"]:
                plan.append(event)
            count += 1

        scenario.run(Context(root, job["seed"], marker))
        if action == "cut":
            raise ValueError("scenario completed before requested marker")
        return {"items": plan, "total": count}
    adapter = load_module(job["adapter"], "crashcheck_adapter")
    trace = Path(job["trace"])
    if action == "record":
        adapter.record(scenario, ctx, trace)
        items = list(itertools.islice(adapter.recipes(trace, ctx.seed), job["limit"] + 1))
        return {"items": items[:job["limit"]], "truncated": len(items) > job["limit"]}
    if action == "materialize":
        candidate = adapter.materialize(Path(job["base"]), trace, job["recipe"], root)
        history = History.decode(candidate["history"])
        return {"cut": candidate["cut"], "history": asdict(history)}
    raise ValueError(f"unknown worker action {action}")


def main():
    job, reply = map(Path, sys.argv[1:])
    try:
        write_json(reply, {"value": execute(read_json(job), reply)})
    except Exception as exc:
        write_json(reply, {"error": inconclusive(f"{type(exc).__name__}: {exc}")})


if __name__ == "__main__":
    main()
