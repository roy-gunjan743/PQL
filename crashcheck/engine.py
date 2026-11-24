"""Fixture orchestration, fresh verification and failure-bundle replay."""
import os
import hashlib
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import time
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
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [sys.executable, "-P", "-m", "crashcheck.worker", str(job), str(reply)],
                cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                creationflags=flags, start_new_session=os.name != "nt",
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.killpg(process.pid, 9)
                process.wait()
                raise WorkerError(f"{action} exceeded {timeout:g} seconds") from exc
        except OSError as exc:
            raise WorkerError(f"{action} worker failed to start: {exc}") from exc
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


def save_bundle(dest, raw, scenario, record, report, base=None, trace=None, adapter=None):
    dest.mkdir(parents=True)
    shutil.copytree(raw, dest / "raw")
    shutil.copyfile(scenario, dest / "scenario.py")
    bundle = {
        "version": VERSION, "seed": report["seed"], "mode": report["mode"],
        "scenario_sha256": report["scenario_sha256"], "base_fingerprint": report["base_fingerprint"],
        "candidate": record,
        "crashcheck_version": "0.2", "environment": report["environment"],
        "configuration": {"seed": report["seed"], "mode": report["mode"],
                          "checker_sha256": report["checker_sha256"]},
        "original_outcome": record["outcome"],
    }
    if adapter:
        shutil.copytree(base, dest / "base")
        shutil.copytree(trace, dest / "trace")
        shutil.copyfile(adapter, dest / "adapter.py")
        bundle["model_evidence"] = {
            "base_fingerprint": fingerprint(inventory(dest / "base")),
            "trace_fingerprint": fingerprint(inventory(dest / "trace")),
            "adapter_sha256": file_hash(dest / "adapter.py"),
        }
    write_json(dest / "bundle.json", bundle)


def explore(report, out, scratch, scenario, adapter, limit, timeout, generation_limit=None,
            wall_seconds=None):
    seed = report["seed"]
    generation_limit = generation_limit or limit
    deadline = time.monotonic() + wall_seconds if wall_seconds else None
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
        plan = invoke("record", control, scenario, seed, timeout, limit=generation_limit, **adapter_args)
        truncated = plan["truncated"]
    else:
        plan = invoke("discover", control, scenario, seed, timeout, limit=generation_limit)
        report["discovered"] = plan["total"]
        truncated = plan["total"] > limit

    report["budget_exhausted"] = truncated
    report["control"] = verify(control, scenario, seed, plan["history"], timeout)
    if report["control"]["status"] != "pass":
        raise WorkerError("uninterrupted control failed; fixture is invalid")
    report["generated"] = len(plan["items"])
    def priority(item):
        name = str(item.get("name", item)).lower()
        return 0 if any(word in name for word in ("ack", "commit", "sync")) else 1
    ordered = sorted(plan["items"], key=lambda item: (priority(item), canonical(item)))
    if len(ordered) > limit:
        chosen = sorted(random.Random(seed).sample(range(len(ordered)), limit))
        ordered = [ordered[i] for i in chosen]
        report["budget_exhausted"] = True
        report["plan"] = "sampled"
    else:
        report["plan"] = "truncated" if truncated else "exhaustive"
    cache = {}
    states = set()
    for index, recipe in enumerate(ordered):
        if deadline and time.monotonic() >= deadline:
            report["budget_exhausted"] = True
            break
        identity = hashlib.sha256(canonical([report["scenario_sha256"], seed, recipe])).hexdigest()[:12]
        record = {"index": index, "id": f"c_{identity}", "recipe": recipe}
        report["candidates"].append(record)
        report["attempted"] += 1
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
                key = (digest, canonical(event["history"]), report["checker_sha256"])
                if key in cache:
                    previous = cache[key]
                    record.update(outcome=previous["outcome"], reused_from=previous["index"])
                    if "bundle" in previous:
                        record["bundle"] = previous["bundle"]
                    report["deduplicated"] += 1
                else:
                    report["verifications"] += 1
                    record["outcome"] = verify(raw, scenario, seed, event["history"], timeout)
                    if record["outcome"]["status"] == "verified-failure":
                        record["bundle"] = f"failures/{index:05d}"
                        save_bundle(out / record["bundle"], raw, scenario, record, report,
                                    base, trace if adapter else None, adapter)
                    if record["outcome"]["status"] != "inconclusive":
                        cache[key] = record
            except (WorkerError, OSError, ValueError) as exc:
                record["outcome"] = inconclusive(str(exc))
        report["unique_states"] = len(states)
        report["checked"] = report["verifications"]
        write_json(out / "manifest.json", report)


def run(scenario, out, seed, limit, timeout, adapter=None, generation_limit=None,
        wall_seconds=None):
    # Reserve the output directory before taking ownership of its contents.
    out.mkdir(parents=True, exist_ok=False)
    report = {
        "version": VERSION, "mode": "modeled" if adapter else "process", "seed": seed,
        "max_candidates": limit, "worker_timeout_seconds": timeout,
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.system()},
        "status": "running", "candidates": [], "verifications": 0, "unique_states": 0,
        "budget_exhausted": False,
        "attempted": 0, "generated": 0, "deduplicated": 0, "checked": 0,
        "max_generated": generation_limit or limit, "wall_seconds": wall_seconds,
    }
    write_json(out / "manifest.json", report)
    try:
        with TemporaryDirectory(prefix="crashcheck-run-") as tmp:
            scratch = Path(tmp)
            pinned = scratch / "scenario.py"
            shutil.copyfile(scenario, pinned)
            report["scenario_sha256"] = file_hash(pinned)
            report["checker_sha256"] = hashlib.sha256(canonical([
                report["scenario_sha256"], report["mode"], report["environment"]["sqlite"],
                report["environment"]["python"]])).hexdigest()
            if adapter:
                local_adapter = scratch / "adapter.py"
                shutil.copyfile(adapter, local_adapter)
                adapter = local_adapter
                report["adapter_sha256"] = file_hash(adapter)
            explore(report, out, scratch, pinned, adapter, limit, timeout,
                    generation_limit, wall_seconds)
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


def replay(bundle, timeout, candidate_id=None):
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
           for p in [bundle, *bundle.rglob("*")]):
        return inconclusive("bundle contains a link")
    if sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()) > 512 * 1024 * 1024:
        return inconclusive("bundle exceeds size limit")
    meta = read_json(bundle / "bundle.json")
    if meta["version"] != VERSION:
        return inconclusive("unsupported bundle version")
    raw, scenario = bundle / "raw", bundle / "scenario.py"
    item = meta["candidate"]
    if candidate_id and item.get("id") != candidate_id:
        return inconclusive("candidate ID mismatch")
    environment = meta.get("environment")
    if environment and (environment.get("sqlite") != sqlite3.sqlite_version or
                        environment.get("python") != platform.python_version()):
        return inconclusive("bundle runtime differs from current Python or SQLite")
    if not raw.is_dir() or fingerprint(inventory(raw)) != item["fingerprint"]:
        return inconclusive("candidate fingerprint mismatch")
    if file_hash(scenario) != meta["scenario_sha256"]:
        return inconclusive("scenario fingerprint mismatch")
    model = meta.get("model_evidence")
    if model:
        base, trace, adapter = bundle / "base", bundle / "trace", bundle / "adapter.py"
        if (fingerprint(inventory(base)) != model["base_fingerprint"] or
                fingerprint(inventory(trace)) != model["trace_fingerprint"] or
                file_hash(adapter) != model["adapter_sha256"]):
            return inconclusive("model evidence fingerprint mismatch")
        with TemporaryDirectory(prefix="crashcheck-regenerate-") as tmp:
            regenerated = Path(tmp) / "raw"
            regenerated.mkdir()
            regenerated_event = invoke("materialize", regenerated, scenario, meta["seed"], timeout,
                                       base=str(base), trace=str(trace), adapter=str(adapter),
                                       recipe=item["recipe"])
            if (fingerprint(inventory(regenerated)) != item["fingerprint"] or
                    regenerated_event["history"] != item["history"]):
                return inconclusive("model regeneration differs from saved candidate")
    History.decode(item["history"])
    actual = verify(raw, scenario, meta["seed"], item["history"], timeout)
    expected = item["outcome"]
    if any(actual.get(k) != expected.get(k) for k in ("status", "failure_class", "check_id")):
        return dict(inconclusive("saved outcome did not reproduce"), observed=actual, expected=expected)
    return dict(actual, reproduced=True, fingerprint=item["fingerprint"])
