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
from .evidence import canonical, failure, inconclusive, read_json, write_json, inventory, fingerprint

CUT_EXIT = 77


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify(scenario, ctx, history):
    checks = []
    evidence = {}
    def add(stage, check_id, status, detail="", failure_class=None):
        entry = dict(stage=stage, check_id=check_id, status=status, detail=detail)
        if failure_class:
            entry["failure_class"] = failure_class
        checks.append(entry)
        return entry
    def result_outcome(status, entry=None):
        value = dict(status=status, checks=checks, evidence=evidence)
        if entry:
            value.update(check_id=entry["check_id"], failure_class=entry.get("failure_class"),
                         detail=entry["detail"])
        return value
    def integrity(stage):
        with ctx.connect() as db:
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            rows = [r[0] for r in db.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                return add(stage, "sqlite.integrity", "verified-failure", "\n".join(rows), "corruption")
            add(stage, "sqlite.integrity", "pass")
            if getattr(scenario, "check_foreign_keys", False):
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    return add(stage, "sqlite.foreign_keys", "verified-failure",
                               repr(violations), "foreign-key")
                add(stage, "sqlite.foreign_keys", "pass")
        return None
    try:
        problem = integrity("open")
        if problem:
            return result_outcome("verified-failure", problem)
    except sqlite3.DatabaseError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return result_outcome("verified-failure", add("open", "sqlite.integrity", "verified-failure",
                                                  str(exc), "corruption"))
        raise
    if hasattr(scenario, "recover"):
        before = fingerprint(inventory(ctx.root))
        try:
            scenario.recover(ctx)
        except CheckFailure as exc:
            return result_outcome("verified-failure", add("app-recovery", exc.check_id,
                          "verified-failure", str(exc), exc.failure_class))
        except Exception as exc:
            return result_outcome("inconclusive", add("app-recovery", "app.recover", "inconclusive",
                          f"{type(exc).__name__}: {exc}", "recovery-error"))
        add("app-recovery", "app.recover", "pass")
        if fingerprint(inventory(ctx.root)) != before:
            problem = integrity("post-recovery")
            if problem:
                return result_outcome("verified-failure", problem)
    before = fingerprint(inventory(ctx.root))
    try:
        result = scenario.observe(ctx, history)
    except CheckFailure as exc:
        stage = "acknowledged-durability" if exc.failure_class == "durability" else "app-validity"
        finding = add(stage, exc.check_id, "verified-failure", str(exc), exc.failure_class)
        result = None
    except Exception as exc:
        finding = add("app-validity", "app.observe", "inconclusive",
                      f"{type(exc).__name__}: {exc}", "observer-error")
        result = None
    else:
        finding = None
    if fingerprint(inventory(ctx.root)) != before:
        return result_outcome("inconclusive", add("harness", "observe.readonly", "inconclusive",
                               "observe modified candidate files", "harness-error"))
    if result is not None:
        raise ValueError("observe must return None or raise CheckFailure")
    if finding:
        return result_outcome(finding["status"], finding)
    add("app-validity", "app.observe", "pass")
    return result_outcome("pass")


def execute(job, reply):
    scenario = load_module(job["scenario"], "crashcheck_scenario")
    root = Path(job["root"])
    ctx = Context(root, job["seed"])
    ctx.allow_concurrent = getattr(scenario, "allow_concurrent", False)
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

        run_ctx = Context(root, job["seed"], marker)
        run_ctx.allow_concurrent = ctx.allow_concurrent
        scenario.run(run_ctx)
        if action == "cut":
            raise ValueError("scenario completed before requested marker")
        return {"items": plan, "total": count, "history": asdict(run_ctx.history)}
    adapter = load_module(job["adapter"], "crashcheck_adapter")
    trace = Path(job["trace"])
    if action == "record":
        adapter.record(scenario, ctx, trace)
        items = list(itertools.islice(adapter.recipes(trace, ctx.seed), job["limit"] + 1))
        return {"items": items[:job["limit"]], "truncated": len(items) > job["limit"],
                "history": asdict(ctx.history)}
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
