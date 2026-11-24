"""Fixture orchestration, fresh verification and failure-bundle replay."""
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from . import History
from .evidence import canonical, file_hash, fingerprint, inconclusive, inventory, read_json, write_json
from .worker import CUT_EXIT

VERSION = 1


class WorkerError(Exception):
    pass


def invoke(action, root, scenario, seed, timeout, **settings):
    with TemporaryDirectory(prefix="crashcheck-job-") as tmp:
        job = Path(tmp) / "job.json"
        reply = Path(tmp) / "reply.json"
        write_json(job, dict(action=action, root=str(root), scenario=str(scenario),
                             seed=seed, **settings))
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                   PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
        try:
            process = subprocess.run(
                [sys.executable, "-P", "-m", "crashcheck.worker", str(job), str(reply)],
                cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerError(f"{action} exceeded {timeout:g} seconds") from exc
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


def save_bundle(dest, raw, scenario, record, report):
    dest.mkdir(parents=True)
    shutil.copytree(raw, dest / "raw")
    shutil.copyfile(scenario, dest / "scenario.py")
    write_json(dest / "bundle.json", {
        "version": VERSION, "seed": report["seed"], "mode": report["mode"],
        "scenario_sha256": report["scenario_sha256"], "base_fingerprint": report["base_fingerprint"],
        "candidate": record,
    })


def explore(report, out, scratch, scenario, adapter, limit, timeout):
    seed = report["seed"]
    base = scratch / "base"
    base.mkdir()
    invoke("setup", base, scenario, seed, timeout)
    report["base_fingerprint"] = fingerprint(inventory(base))
    control = scratch / "control"
    shutil.copytree(base, control)
    adapter_args = {}
    if adapter:
        trace = out / "trace"
        trace.mkdir()
        adapter_args = dict(adapter=str(adapter), trace=str(trace))
        plan = invoke("record", control, scenario, seed, timeout, limit=limit, **adapter_args)
        truncated = plan["truncated"]
    else:
        plan = invoke("discover", control, scenario, seed, timeout, limit=limit)
        report["discovered"] = plan["total"]
        truncated = plan["total"] > limit

    report["budget_exhausted"] = truncated
    cache = {}
    states = set()
    for index, recipe in enumerate(plan["items"]):
        record = {"index": index, "recipe": recipe}
        report["candidates"].append(record)
        with TemporaryDirectory(dir=scratch) as candidate_tmp:
            raw = Path(candidate_tmp) / "raw"
            try:
                if adapter:
                    raw.mkdir()
                    event = invoke("materialize", raw, scenario, seed, timeout, recipe=recipe,
                                   base=str(base), **adapter_args)
                    record["cut"] = event["cut"]
                else:
                    shutil.copytree(base, raw)
                    event = invoke("cut", raw, scenario, seed, timeout, cut=recipe)
                    record["cut"] = event["name"]
                record["history"] = event["history"]
                entries = inventory(raw)
                digest = fingerprint(entries)
                states.add(digest)
                record["fingerprint"] = digest
                key = (digest, canonical(event["history"]))
                if key in cache:
                    previous = cache[key]
                    record.update(outcome=previous["outcome"], reused_from=previous["index"])
                    if "bundle" in previous:
                        record["bundle"] = previous["bundle"]
                else:
                    report["verifications"] += 1
                    record["outcome"] = verify(raw, scenario, seed, event["history"], timeout)
                    if record["outcome"]["status"] == "verified-failure":
                        record["bundle"] = f"failures/{index:05d}"
                        save_bundle(out / record["bundle"], raw, scenario, record, report)
                    if record["outcome"]["status"] != "inconclusive":
                        cache[key] = record
            except (WorkerError, OSError, ValueError) as exc:
                record["outcome"] = inconclusive(str(exc))
        report["unique_states"] = len(states)
        write_json(out / "manifest.json", report)


def run(scenario, out, seed, limit, timeout, adapter=None):
    # Reserve the output directory before taking ownership of its contents.
    out.mkdir(parents=True, exist_ok=False)
    report = {
        "version": VERSION, "mode": "modeled" if adapter else "process", "seed": seed,
        "max_candidates": limit, "worker_timeout_seconds": timeout,
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.system()},
        "status": "running", "candidates": [], "verifications": 0, "unique_states": 0,
        "budget_exhausted": False,
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
            explore(report, out, scratch, pinned, adapter, limit, timeout)
    except (WorkerError, OSError, ValueError) as exc:
        report["detail"] = str(exc)
    outcomes = [entry["outcome"]["status"] for entry in report["candidates"] if "outcome" in entry]
    report["counts"] = {s: outcomes.count(s) for s in ("pass", "verified-failure", "inconclusive")}
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


def replay(bundle, timeout):
    meta = read_json(bundle / "bundle.json")
    if meta["version"] != VERSION:
        return inconclusive("unsupported bundle version")
    raw, scenario = bundle / "raw", bundle / "scenario.py"
    item = meta["candidate"]
    if not raw.is_dir() or fingerprint(inventory(raw)) != item["fingerprint"]:
        return inconclusive("candidate fingerprint mismatch")
    if file_hash(scenario) != meta["scenario_sha256"]:
        return inconclusive("scenario fingerprint mismatch")
    History.decode(item["history"])
    actual = verify(raw, scenario, meta["seed"], item["history"], timeout)
    expected = item["outcome"]
    if any(actual.get(k) != expected.get(k) for k in ("status", "failure_class", "check_id")):
        return dict(inconclusive("saved outcome did not reproduce"), observed=actual, expected=expected)
    return dict(actual, reproduced=True, fingerprint=item["fingerprint"])
