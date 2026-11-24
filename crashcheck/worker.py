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
from .evidence import (canonical, check_result, failure, fingerprint, inconclusive,
                       inventory, read_json, write_json)

CUT_EXIT = 77


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def integrity_stage(scenario, ctx, checks, fail, ok, label=""):
    """Open the candidate, force journal recovery, run integrity and foreign-key checks."""
    # mode=rw allows SQLite journal recovery without creating a missing database.
    try:
        db = sqlite3.connect(ctx.db.as_uri() + "?mode=rw", uri=True)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            # An explicit first read lets SQLite roll back a hot journal before any check.
            db.execute("SELECT count(*) FROM sqlite_master").fetchall()
            if not label:
                ok("open", "sqlite.open")
            rows = [r[0] for r in db.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                return fail("integrity" + label, "sqlite.integrity" + label.replace("-", "_"),
                            "corruption", "\n".join(rows))
            ok("integrity" + label, "sqlite.integrity" + label.replace("-", "_"))
            if getattr(scenario, "CHECK_FOREIGN_KEYS", False):
                bad = db.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    return fail("foreign-keys" + label, "sqlite.foreign_keys", "foreign-key",
                                f"{len(bad)} violation(s), first: {tuple(bad[0])!r}")
                ok("foreign-keys" + label, "sqlite.foreign_keys")
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return fail("open" if not label else "integrity" + label, "sqlite.integrity",
                        "corruption", str(exc))
        raise
    return None


def verify(scenario, ctx, history):
    """Staged verification: open, integrity, foreign keys, app recovery, observe.

    Each stage appends to ``checks``. Open and integrity results are recorded before
    the application hook runs. Harness errors (anything not a verified finding) raise.
    """
    checks = []

    def ok(stage, check_id):
        checks.append(check_result(stage, check_id, "pass"))

    def fail(stage, check_id, failure_class, message):
        checks.append(check_result(stage, check_id, "fail", failure_class, message))
        return failure(check_id, failure_class, message, stage=stage, checks=checks)

    early = integrity_stage(scenario, ctx, checks, fail, ok)
    if early:
        return early

    before = fingerprint(inventory(ctx.root))
    if hasattr(scenario, "recover"):
        try:
            scenario.recover(ctx)
        except CheckFailure as exc:
            return fail("app-recovery", exc.check_id, exc.failure_class, str(exc))
        except Exception as exc:  # the application could not recover this candidate
            return fail("app-recovery", "app.recovery", "recovery-error",
                        f"{type(exc).__name__}: {exc}")
        ok("app-recovery", "app.recovery")
        after = fingerprint(inventory(ctx.root))
        if after != before:  # a mutating hook: integrity must hold again
            early = integrity_stage(scenario, ctx, checks, fail, ok, label="-after-recovery")
            if early:
                return early
            before = fingerprint(inventory(ctx.root))

    finding = None
    try:
        result = scenario.observe(ctx, history)
    except CheckFailure as exc:
        finding, result = exc, None
    if fingerprint(inventory(ctx.root)) != before:
        raise ValueError("observe modified the candidate; it must be read-only "
                         "(open the database with ctx.connect(readonly=True))")
    if finding is not None:
        return fail("observe", finding.check_id, finding.failure_class, str(finding))
    if result is not None:
        raise ValueError("observe must return None or raise CheckFailure")
    ok("observe", "app.observe")
    return {"status": "pass", "checks": checks}


def execute(job, reply):
    scenario = load_module(job["scenario"], "crashcheck_scenario")
    root = Path(job["root"])
    strict = getattr(scenario, "STRICT_ORDERING", True)
    ctx = Context(root, job["seed"], strict_ordering=strict)
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
                # The receipt is written atomically before the abrupt exit; the parent
                # verifies both the receipt and the exit status before freezing files.
                write_json(reply, {"value": event})
                os._exit(CUT_EXIT)
            if action == "discover":
                plan.append(event)
            count += 1

        run_ctx = Context(root, job["seed"], marker, strict_ordering=strict)
        scenario.run(run_ctx)
        if action == "cut":
            raise ValueError("scenario completed before requested marker")
        return {"items": plan, "total": count, "final_history": asdict(run_ctx.history)}
    adapter = load_module(job["adapter"], "crashcheck_adapter")
    trace = Path(job["trace"])
    if action == "record":
        adapter.record(scenario, ctx, trace)
        items = list(itertools.islice(adapter.recipes(trace, ctx.seed), job["limit"] + 1))
        return {"items": items[:job["limit"]], "truncated": len(items) > job["limit"],
                "final_history": asdict(ctx.history)}
    if action == "materialize":
        candidate = adapter.materialize(Path(job["base"]), trace, job["recipe"], root)
        history = History.decode(candidate["history"])
        value = {"cut": candidate["cut"], "history": asdict(history)}
        if "effects" in candidate:
            value["effects"] = candidate["effects"]
        return value
    if action == "reductions":
        if not hasattr(adapter, "reductions"):
            return {"items": []}
        items = list(itertools.islice(adapter.reductions(trace, job["recipe"]), job["limit"]))
        return {"items": items}
    raise ValueError(f"unknown worker action {action}")


def main():
    job, reply = map(Path, sys.argv[1:])
    try:
        write_json(reply, {"value": execute(read_json(job), reply)})
    except Exception as exc:
        write_json(reply, {"error": inconclusive(f"{type(exc).__name__}: {exc}")})


if __name__ == "__main__":
    main()
