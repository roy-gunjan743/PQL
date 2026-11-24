"""Fixture orchestration, scheduling, fresh verification, bundles, replay and minimization."""
import hashlib
import os
import platform
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from . import History, __version__
from .evidence import canonical, file_hash, fingerprint, inconclusive, inventory, read_json, write_json
from .worker import CUT_EXIT

VERSION = 1  # failure-bundle format. New fields are additive; replay accepts older v1 bundles.
CHECKER_VERSION = 2
PRIORITY = re.compile(r"ack|commit|sync|journal|truncate|delete|rename", re.IGNORECASE)

MAX_META_BYTES = 8 * 1024 * 1024
MAX_SCENARIO_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 1024 * 1024 * 1024


class WorkerError(Exception):
    pass


def reap(process):
    """Kill the worker's whole process group, including grandchildren, then collect it."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def invoke(action, root, scenario, seed, timeout, **settings):
    with TemporaryDirectory(prefix="crashcheck-job-") as tmp:
        job = Path(tmp) / "job.json"
        reply = Path(tmp) / "reply.json"
        write_json(job, dict(action=action, root=str(root), scenario=str(scenario),
                             seed=seed, **settings))
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                   PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "crashcheck.worker", str(job), str(reply)],
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            reap(process)
            raise WorkerError(f"{action} exceeded {timeout:g} seconds") from exc
        finally:
            reap(process)  # no descendant outlives its worker
        if not reply.is_file():
            raise WorkerError(f"{action} worker exited {process.returncode} without a result")
        result = read_json(reply)
        if "error" in result:
            raise WorkerError(f"{action}: {result['error']['detail']}")
        expected = CUT_EXIT if action == "cut" else 0
        if process.returncode != expected:
            raise WorkerError(f"{action} worker exited {process.returncode}, expected {expected}")
        return result["value"]


def verify(raw, scenario, seed, history, timeout):
    with TemporaryDirectory(prefix="crashcheck-recovery-") as tmp:
        copy = Path(tmp) / "fixture"
        shutil.copytree(raw, copy)
        return invoke("verify", copy, scenario, seed, timeout, history=history)


def candidate_id(identity, index, recipe):
    """Stable ID: readable ordinal plus a hash of run identity and recipe."""
    digest = hashlib.sha256(canonical({"run": identity, "recipe": recipe})).hexdigest()
    return f"c_{index:03d}-{digest[:8]}"


def id_matches(actual, wanted):
    return bool(actual) and (actual == wanted or actual.split("-")[0] == wanted)


def schedule(events, limit, seed):
    """Deterministic process-mode plan.

    Cuts whose marker names mention acknowledgement, commit, sync, journal, truncate,
    delete or rename come first, then the rest, each group in encounter order. When the
    space exceeds the budget, each group is sampled with a seeded generator and the
    chosen cuts stay in encounter order within their group.
    """
    groups = [[e for e in events if PRIORITY.search(e["name"])],
              [e for e in events if not PRIORITY.search(e["name"])]]
    if len(events) <= limit:
        return groups[0] + groups[1], "exhaustive"
    rng = random.Random(f"crashcheck-schedule:{seed}")
    plan, room = [], limit
    for group in groups:
        shuffled = list(group)
        rng.shuffle(shuffled)
        chosen = sorted(shuffled[:room], key=lambda e: e["index"])
        plan.extend(chosen)
        room -= len(chosen)
    return plan, "sampled"


def save_bundle(dest, raw, scenario, record, report, adapter=None, base=None, trace=None):
    dest.mkdir(parents=True)
    shutil.copytree(raw, dest / "raw")
    shutil.copyfile(scenario, dest / "scenario.py")
    meta = {
        "version": VERSION, "seed": report["seed"], "mode": report["mode"],
        "scenario_sha256": report["scenario_sha256"], "base_fingerprint": report["base_fingerprint"],
        "candidate": record,
        "crashcheck": __version__, "run_identity": report.get("run_identity"),
        "runtime": report["environment"], "config": report.get("config", {}),
        "context": {"cut": record.get("cut"), "history": record.get("history")},
    }
    if adapter:
        shutil.copyfile(adapter, dest / "adapter.py")
        meta["adapter_sha256"] = report["adapter_sha256"]
        if base is not None:
            shutil.copytree(base, dest / "base")
            meta["base_inventory"] = inventory(base)
        if trace is not None:
            shutil.copytree(trace, dest / "trace")
    write_json(dest / "bundle.json", meta)


def explore(report, out, scratch, scenario, adapter, limit, timeout, max_checks, deadline):
    seed = report["seed"]
    base = scratch / "base"
    base.mkdir()
    invoke("setup", base, scenario, seed, timeout)
    report["base_fingerprint"] = fingerprint(inventory(base))
    report["run_identity"] = hashlib.sha256(canonical({
        "scenario": report["scenario_sha256"], "adapter": report.get("adapter_sha256"),
        "seed": seed, "mode": report["mode"], "base": report["base_fingerprint"],
    })).hexdigest()
    identity = report["run_identity"]
    config_key = hashlib.sha256(canonical({
        "scenario": report["scenario_sha256"], "adapter": report.get("adapter_sha256"),
        "checker": CHECKER_VERSION})).hexdigest()
    control = scratch / "control"
    shutil.copytree(base, control)
    adapter_args = {}
    if adapter:
        trace = out / "trace"
        trace.mkdir()
        adapter_args = dict(adapter=str(adapter), trace=str(trace))
        plan = invoke("record", control, scenario, seed, timeout, limit=limit, **adapter_args)
        items, truncated = plan["items"], plan["truncated"]
        generated = len(items) + (1 if truncated else 0)
        search_mode = "adapter-ordered"
    else:
        plan = invoke("discover", control, scenario, seed, timeout)
        report["discovered"] = plan["total"]
        items, search_mode = schedule(plan["items"], limit, seed)
        truncated = plan["total"] > limit
        generated = plan["total"]
    report["budget_exhausted"] = truncated
    if truncated:
        report["budget_reason"] = "candidate limit"
    report["search"] = {"mode": search_mode, "generated": generated, "attempted": 0,
                        "deduplicated": 0, "checked": 0}

    # Clean control: the uninterrupted final state must not be a verified failure.
    try:
        control_outcome = verify(control, scenario, seed, plan["final_history"], timeout)
    except WorkerError as exc:
        control_outcome = inconclusive(str(exc))
    report["control"] = control_outcome
    if control_outcome["status"] == "verified-failure":
        report["detail"] = ("clean control failed, so the fixture or checks are invalid: "
                            f"{control_outcome['check_id']}: {control_outcome['detail']}")
        return

    cache = {}
    states = set()
    for index, recipe in enumerate(items):
        if deadline is not None and time.monotonic() > deadline:
            report["budget_exhausted"], report["budget_reason"] = True, "time budget"
            break
        record = {"index": index, "id": candidate_id(identity, index, recipe), "recipe": recipe}
        report["candidates"].append(record)
        report["search"]["attempted"] += 1
        with TemporaryDirectory(dir=scratch) as candidate_tmp:
            raw = Path(candidate_tmp) / "raw"
            try:
                if adapter:
                    raw.mkdir()
                    event = invoke("materialize", raw, scenario, seed, timeout, recipe=recipe,
                                   base=str(base), **adapter_args)
                    record["cut"] = event["cut"]
                    if "effects" in event:
                        record["effect_counts"] = event["effects"]
                else:
                    shutil.copytree(base, raw)
                    event = invoke("cut", raw, scenario, seed, timeout, cut=recipe)
                    record["cut"] = event["name"]
                record["history"] = event["history"]
                digest = fingerprint(inventory(raw))
                states.add(digest)
                record["fingerprint"] = digest
                key = (digest, canonical(event["history"]), config_key)
                if key in cache:
                    previous = cache[key]
                    record.update(outcome=previous["outcome"], reused_from=previous["index"],
                                  reused_from_id=previous["id"])
                    report["search"]["deduplicated"] += 1
                    if "bundle" in previous:
                        record["bundle"] = previous["bundle"]
                else:
                    if max_checks is not None and report["verifications"] >= max_checks:
                        report["budget_exhausted"], report["budget_reason"] = True, "check limit"
                        record["skipped"] = "check budget exhausted"
                        break
                    report["verifications"] += 1
                    report["search"]["checked"] += 1
                    record["outcome"] = verify(raw, scenario, seed, event["history"], timeout)
                    if record["outcome"]["status"] == "verified-failure":
                        record["bundle"] = f"failures/{index:05d}"
                        save_bundle(out / record["bundle"], raw, scenario, record, report,
                                    adapter=adapter, base=base if adapter else None,
                                    trace=out / "trace" if adapter else None)
                    if record["outcome"]["status"] != "inconclusive":
                        cache[key] = record
            except (WorkerError, OSError, ValueError) as exc:
                record["outcome"] = inconclusive(str(exc))
        report["unique_states"] = len(states)
        write_json(out / "manifest.json", report)
    report["unique_states"] = len(states)


def run(scenario, out, seed, limit, timeout, adapter=None, max_checks=None, time_budget=None,
        model=None):
    # Reserve the output directory before taking ownership of its contents.
    out.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + time_budget if time_budget is not None else None
    report = {
        "version": VERSION, "mode": "modeled" if adapter else "process", "seed": seed,
        "max_candidates": limit, "worker_timeout_seconds": timeout,
        "config": {"max_candidates": limit, "max_checks": max_checks,
                   "time_budget_seconds": time_budget, "model": model},
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.system(), "crashcheck": __version__},
        "status": "running", "candidates": [], "verifications": 0, "unique_states": 0,
        "budget_exhausted": False,
        "search": {"mode": None, "generated": 0, "attempted": 0, "deduplicated": 0, "checked": 0},
    }
    write_json(out / "manifest.json", report)
    try:
        with TemporaryDirectory(prefix="crashcheck-run-") as tmp:
            scratch = Path(tmp)
            pinned = scratch / "scenario.py"
            shutil.copyfile(scenario, pinned)
            report["scenario_sha256"] = file_hash(pinned)
            if adapter:
                local_adapter = scratch / "adapter.py"
                shutil.copyfile(adapter, local_adapter)
                adapter = local_adapter
                report["adapter_sha256"] = file_hash(adapter)
            explore(report, out, scratch, pinned, adapter, limit, timeout, max_checks, deadline)
    except (WorkerError, OSError, ValueError) as exc:
        report["detail"] = str(exc)
    outcomes = [entry["outcome"]["status"] for entry in report["candidates"] if "outcome" in entry]
    report["counts"] = {s: outcomes.count(s) for s in ("pass", "verified-failure", "inconclusive")}
    # A verified failure takes precedence over a partial exploration.
    if "verified-failure" in outcomes:
        report["status"] = "verified-failure"
    elif not outcomes or "detail" in report or "inconclusive" in outcomes or report["budget_exhausted"]:
        report["status"] = "inconclusive"
        if not outcomes and "detail" not in report:
            report["detail"] = "no candidate cuts were available"
    else:
        report["status"] = "pass"
    write_json(out / "manifest.json", report)
    return report


# ---------------------------------------------------------------- bundles and replay

def check_tree(root, label):
    """Reject links and oversized trees before reading anything from a bundle."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                raise ValueError(f"bundle {label} contains a link: {path.relative_to(root)}")
        for name in filenames:
            total += (Path(dirpath) / name).stat().st_size
        if total > MAX_TREE_BYTES:
            raise ValueError(f"bundle {label} is larger than {MAX_TREE_BYTES} bytes")


def check_bundle(bundle):
    if bundle.is_symlink():
        raise ValueError("bundle path is a link")
    for name, limit in (("bundle.json", MAX_META_BYTES), ("scenario.py", MAX_SCENARIO_BYTES)):
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"bundle is missing {name}")
        if path.stat().st_size > limit:
            raise ValueError(f"bundle {name} is larger than {limit} bytes")
    for name in ("raw", "base", "trace"):
        path = bundle / name
        if path.is_symlink():
            raise ValueError(f"bundle {name} is a link")
        if path.is_dir():
            check_tree(path, name)
    adapter = bundle / "adapter.py"
    if adapter.is_symlink():
        raise ValueError("bundle adapter.py is a link")


def find_bundle(path, candidate=None):
    """Accept a bundle directory, or a run directory plus --candidate."""
    path = Path(path)
    if (path / "bundle.json").is_file():
        if candidate:
            check_bundle(path)
            found = read_json(path / "bundle.json")["candidate"].get("id")
            if not id_matches(found, candidate):
                raise ValueError(f"bundle holds candidate {found}, not {candidate}")
        return path
    failures = path / "failures"
    if not failures.is_dir() or failures.is_symlink():
        raise ValueError("expected a failure bundle or a run directory with failures")
    matches = []
    for entry in sorted(failures.iterdir()):
        if entry.is_symlink() or not (entry / "bundle.json").is_file():
            continue
        found = read_json(entry / "bundle.json")["candidate"].get("id")
        if candidate is None or id_matches(found, candidate):
            matches.append(entry)
    if not matches:
        raise ValueError(f"no failure bundle for candidate {candidate}")
    if candidate is None and len(matches) > 1:
        raise ValueError(f"run directory has {len(matches)} failure bundles; pass --candidate")
    return matches[0]


def same_outcome(actual, expected):
    return all(actual.get(k) == expected.get(k) for k in ("status", "failure_class", "check_id"))


def regenerate(bundle, meta, item, timeout):
    """Rebuild the raw candidate from base, trace and recipe in a fresh worker."""
    adapter, base, trace = bundle / "adapter.py", bundle / "base", bundle / "trace"
    if meta.get("mode") != "modeled" or not (adapter.is_file() and base.is_dir() and trace.is_dir()):
        raise ValueError("bundle has no adapter, base and trace to regenerate from")
    if file_hash(adapter) != meta.get("adapter_sha256"):
        raise ValueError("adapter fingerprint mismatch")
    if fingerprint(inventory(base)) != meta["base_fingerprint"]:
        raise ValueError("base fingerprint mismatch")
    tmp = TemporaryDirectory(prefix="crashcheck-regen-")
    raw = Path(tmp.name) / "raw"
    raw.mkdir()
    event = invoke("materialize", raw, bundle / "scenario.py", meta["seed"], timeout,
                   recipe=item["recipe"], base=str(base), adapter=str(adapter), trace=str(trace))
    if fingerprint(inventory(raw)) != item["fingerprint"] or canonical(event["history"]) != canonical(item["history"]):
        tmp.cleanup()
        raise ValueError("regenerated candidate differs from the saved raw files")
    return tmp, raw, event


def replay(bundle, timeout, candidate=None, regenerate_raw=False, ignore_runtime=False):
    bundle = find_bundle(bundle, candidate)
    check_bundle(bundle)
    meta = read_json(bundle / "bundle.json")
    if meta["version"] != VERSION:
        return inconclusive("unsupported bundle version")
    runtime = meta.get("runtime") or {}
    if not ignore_runtime and runtime.get("sqlite") not in (None, sqlite3.sqlite_version):
        return inconclusive(f"runtime mismatch: bundle used SQLite {runtime['sqlite']}, "
                            f"this runtime has {sqlite3.sqlite_version} (use --ignore-runtime)")
    raw, scenario = bundle / "raw", bundle / "scenario.py"
    item = meta["candidate"]
    if not raw.is_dir() or fingerprint(inventory(raw)) != item["fingerprint"]:
        return inconclusive("candidate fingerprint mismatch")
    if file_hash(scenario) != meta["scenario_sha256"]:
        return inconclusive("scenario fingerprint mismatch")
    History.decode(item["history"])
    held = None
    try:
        if regenerate_raw:
            held, raw, _ = regenerate(bundle, meta, item, timeout)
        actual = verify(raw, scenario, meta["seed"], item["history"], timeout)
        expected = item["outcome"]
        if not same_outcome(actual, expected):
            repeat = verify(raw, scenario, meta["seed"], item["history"], timeout)
            kind = "flaky" if not same_outcome(repeat, actual) else "mismatch"
            return dict(inconclusive(f"saved outcome did not reproduce ({kind})"),
                        classification=kind, observed=actual, observed_repeat=repeat,
                        expected=expected)
        return dict(actual, reproduced=True, fingerprint=item["fingerprint"],
                    candidate=item.get("id"), regenerated=regenerate_raw)
    finally:
        if held is not None:
            held.cleanup()


def minimize(bundle, out, timeout, candidate=None, ignore_runtime=False, max_steps=50):
    """Shrink a modeled failure through adapter-provided ``reductions``.

    The original is reproduced first. A reduction is kept only if it materializes
    legally and reproduces the same check ID and failure class. The original bundle
    and the minimized bundle are saved side by side under ``out``.
    """
    bundle = find_bundle(bundle, candidate)
    baseline = replay(bundle, timeout, ignore_runtime=ignore_runtime)
    if baseline.get("reproduced") is not True:
        return dict(inconclusive("original failure did not reproduce"), replay=baseline)
    meta = read_json(bundle / "bundle.json")
    if meta.get("mode") != "modeled":
        return dict(inconclusive("only modeled bundles have recipes to reduce"), replay=baseline)
    item = meta["candidate"]
    expected = item["outcome"]
    adapter, base, trace = bundle / "adapter.py", bundle / "base", bundle / "trace"
    scenario, seed = bundle / "scenario.py", meta["seed"]
    current, accepted, steps = dict(item), None, 0
    with TemporaryDirectory(prefix="crashcheck-minimize-") as tmp:
        progress = True
        while progress and steps < max_steps:
            progress = False
            options = invoke("reductions", Path(tmp), scenario, seed, timeout, recipe=current["recipe"],
                             adapter=str(adapter), trace=str(trace), limit=100)["items"]
            for recipe in options:
                steps += 1
                raw = Path(tmp) / f"try{steps}"
                raw.mkdir()
                try:
                    event = invoke("materialize", raw, scenario, seed, timeout, recipe=recipe,
                                   base=str(base), adapter=str(adapter), trace=str(trace))
                    outcome = verify(raw, scenario, seed, event["history"], timeout)
                except (WorkerError, ValueError):
                    continue  # an illegal reduction is discarded
                if outcome["status"] == "verified-failure" and same_outcome(outcome, expected):
                    current = dict(current, recipe=recipe, cut=event["cut"], history=event["history"],
                                   fingerprint=fingerprint(inventory(raw)), outcome=outcome)
                    accepted = (raw, current)
                    progress = True
                    break
        out.mkdir(parents=True, exist_ok=False)
        shutil.copytree(bundle, out / "original")
        shutil.copytree(bundle, out / "minimized")
        result = {"status": "verified-failure", "check_id": expected.get("check_id"),
                  "failure_class": expected.get("failure_class"), "minimized": accepted is not None,
                  "original_recipe": item["recipe"], "steps": steps, "out": str(out)}
        if accepted:
            raw, current = accepted
            current["id"] = candidate_id(meta.get("run_identity"), item.get("index", 0), current["recipe"])
            shutil.rmtree(out / "minimized" / "raw")
            shutil.copytree(raw, out / "minimized" / "raw")
            new_meta = dict(meta, candidate=current, minimized_from=item.get("id"))
            write_json(out / "minimized" / "bundle.json", new_meta)
            result["minimized_recipe"] = current["recipe"]
    return result
