import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOKING = (ROOT / "examples" / "booking.py").read_text(encoding="utf-8")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scenario = self.root / "scenario.py"
        self.out = self.root / "run"

    def cli(self, *args):
        proc = subprocess.run([sys.executable, "-m", "crashcheck", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=45)
        return proc.returncode, json.loads(proc.stdout)

    def explore(self, source, *args):
        self.scenario.write_text(source, encoding="utf-8")
        code, _ = self.cli("run", self.scenario, "--out", self.out, *args)
        return code, json.loads((self.out / "manifest.json").read_text())

    def test_split_commit_replays_without_original_scenario(self):
        code, report = self.explore(BOOKING)
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"verified-failure": 2, "pass": 1, "inconclusive": 0})
        item = report["candidates"][0]
        self.assertEqual(item["outcome"]["check_id"], "booking.capacity")
        self.scenario.unlink()
        bundle = self.out / item["bundle"]
        code, replay = self.cli("replay", bundle)
        self.assertEqual((code, replay["reproduced"], replay["fingerprint"]), (1, True, item["fingerprint"]))
        (bundle / "raw" / "extra").touch()
        self.assertEqual(self.cli("replay", bundle)[0], 2)

    def test_atomic_and_skipped_cleanup(self):
        source = "import atexit\n" + BOOKING.replace("SPLIT_COMMIT = True", "SPLIT_COMMIT = False")
        source = source.replace("    ctx.begin(0)", "    atexit.register((ctx.root / 'cleaned').touch)\n    ctx.begin(0)")
        source = source.replace("    if remaining", "    assert not (ctx.root / 'cleaned').exists()\n    if remaining")
        code, report = self.explore(source)
        self.assertEqual((code, report["counts"]["pass"]), (0, 3))

    def test_identical_bytes_distinct_receipt_and_dedup(self):
        source = BOOKING.split("def run(")[0] + """
def run(ctx):
    ctx.begin(0)
    ctx.marker("repeat")
    ctx.marker("repeat")
    ctx.acknowledge(0)
    ctx.marker("repeat")
    with closing(sqlite3.connect(ctx.db)) as db:
        db.execute("INSERT INTO bookings VALUES (0)")
        db.execute("UPDATE seats SET remaining = 0")
        db.commit()
""" + "\ndef observe(" + BOOKING.split("def observe(")[1]
        code, report = self.explore(source)
        self.assertEqual((code, report["unique_states"], report["verifications"]), (1, 1, 2))
        self.assertEqual(report["candidates"][1]["reused_from"], 0)
        self.assertEqual(report["candidates"][2]["outcome"]["check_id"], "booking.receipt")

    def test_budget_and_output_collision(self):
        code, report = self.explore(BOOKING.replace("SPLIT_COMMIT = True", "SPLIT_COMMIT = False"),
                                    "--max-candidates", 1)
        self.assertEqual(code, 2)
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual(report["discovered"], 3)
        self.assertEqual(self.cli("run", self.scenario, "--out", self.out)[0], 2)
        self.assertEqual(report, json.loads((self.out / "manifest.json").read_text()))

    def test_worker_error_and_timeout(self):
        code, report = self.explore(BOOKING.replace("ctx.begin(0)", "raise RuntimeError('setup fault')"))
        self.assertEqual(code, 2)
        self.assertIn("setup fault", report["detail"])
        self.out = self.root / "timeout"
        code, report = self.explore(BOOKING.replace("ctx.begin(0)", "import time; time.sleep(10)"),
                                    "--worker-timeout", 1)
        self.assertEqual(code, 2)
        self.assertIn("exceeded", report["detail"])

    def test_modeled_record_materialize_verify_replay(self):
        adapter = self.root / "adapter.py"
        adapter.write_text("""
import shutil

def record(scenario, ctx, trace):
    scenario.run(ctx)
    (trace / "recorded").write_text("ok")

def recipes(trace, seed):
    assert (trace / "recorded").read_text() == "ok"
    yield {"step": 0}

def materialize(base, trace, recipe, dest):
    shutil.copytree(base, dest, dirs_exist_ok=True)
    return {"cut": recipe["step"], "history": {"begun": [0], "acknowledged": [0]}}
""", encoding="utf-8")
        code, report = self.explore(BOOKING, "--adapter", adapter)
        self.assertEqual((code, report["mode"]), (1, "modeled"))
        item = report["candidates"][0]
        self.assertEqual(item["outcome"]["failure_class"], "durability")
        self.assertTrue(self.cli("replay", self.out / item["bundle"])[1]["reproduced"])





def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scenario = self.root / "scenario.py"
        self.out = self.root / "run"

    def cli(self, *args):
        proc = subprocess.run([sys.executable, "-m", "crashcheck", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        return proc.returncode, json.loads(proc.stdout)

    def explore(self, source, *args, command="test"):
        self.scenario.write_text(source, encoding="utf-8")
        code, _ = self.cli(command, self.scenario, "--out", self.out, *args)
        return code, json.loads((self.out / "manifest.json").read_text())

    def atomic(self):
        return BOOKING.replace("SPLIT_COMMIT = True", "SPLIT_COMMIT = False")

    def test_premature_acknowledgement_fails_durability_with_clean_integrity(self):
        source = BOOKING.split("def run(")[0] + """
def run(ctx):
    ctx.begin(0)
    with closing(sqlite3.connect(ctx.db)) as db:
        db.execute("INSERT INTO bookings VALUES (0)")
        db.execute("UPDATE seats SET remaining = 0")
        ctx.acknowledge(0)
        ctx.marker("acknowledged-before-commit")
        db.commit()
        ctx.marker("after-commit")

def observe(ctx, history):
    with closing(sqlite3.connect(ctx.db)) as db:
        remaining, = db.execute("SELECT remaining FROM seats").fetchone()
        booked = {row[0] for row in db.execute("SELECT operation FROM bookings")}
    if remaining + len(booked) != 1:
        raise CheckFailure("booking.capacity", "invariant", "totals disagree")
    if not set(history.acknowledged).issubset(booked):
        raise CheckFailure("booking.receipt", "durability", "an acknowledged booking is missing")
"""
        code, report = self.explore(source)
        self.assertEqual(code, 1)
        first = report["candidates"][0]["outcome"]
        self.assertEqual((first["failure_class"], first["stage"]), ("durability", "observe"))
        passed = {c["check_id"] for c in first["checks"] if c["status"] == "pass"}
        self.assertEqual(passed, {"sqlite.open", "sqlite.integrity"})
        self.assertEqual(report["counts"]["pass"], 1)

    def test_recovery_hook_error_is_its_own_stage(self):
        source = self.atomic().replace("SPLIT_COMMIT = False", "SPLIT_COMMIT = True") + """

def recover(ctx):
    with closing(sqlite3.connect(ctx.db)) as db:
        remaining, = db.execute("SELECT remaining FROM seats").fetchone()
        booked, = db.execute("SELECT count(*) FROM bookings").fetchone()
    if remaining + booked != 1:
        raise RuntimeError("cannot repair")
"""
        code, report = self.explore(source)
        self.assertEqual((code, report["counts"]["inconclusive"]), (1, 0))
        outcome = report["candidates"][0]["outcome"]
        self.assertEqual((outcome["stage"], outcome["check_id"], outcome["failure_class"]),
                         ("app-recovery", "app.recovery", "recovery-error"))
        self.assertIn("cannot repair", outcome["detail"])

    def test_observe_that_mutates_is_a_harness_error(self):
        source = self.atomic().replace("    if remaining + len(booked) != 1:",
                                       "    db2 = sqlite3.connect(ctx.db)\n    db2.execute('UPDATE seats SET remaining = 5')\n    db2.commit()\n    db2.close()\n    if remaining + len(booked) != 1:")
        code, report = self.explore(source)
        self.assertEqual(code, 2)
        self.assertEqual(report["counts"]["pass"], 0)
        self.assertIn("read-only", json.dumps(report["candidates"]))

    def test_foreign_key_check_runs_when_declared(self):
        source = """
from contextlib import closing
import sqlite3

CHECK_FOREIGN_KEYS = True

def setup(ctx):
    with closing(sqlite3.connect(ctx.db)) as db:
        db.executescript(\"\"\"
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));
            INSERT INTO parent VALUES (1);
            INSERT INTO child VALUES (1, 1);
        \"\"\")

def run(ctx):
    ctx.begin(0)
    with closing(sqlite3.connect(ctx.db)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM parent")
        db.commit()
        ctx.marker("parent-removed")
        db.execute("DELETE FROM child")
        db.commit()
        ctx.marker("child-removed")
    ctx.acknowledge(0)

def observe(ctx, history):
    pass
""".replace('\\"', '"')
        code, report = self.explore(source)
        self.assertEqual(code, 1)
        first, second = report["candidates"]
        self.assertEqual(first["outcome"]["check_id"], "sqlite.foreign_keys")
        self.assertEqual(second["outcome"]["status"], "pass")

    def test_clean_control_failure_invalidates_the_run(self):
        source = self.atomic().replace("db.commit()\n        ctx.acknowledge(0)",
                                       "db.rollback()\n        ctx.acknowledge(0)")
        code, report = self.explore(source)
        self.assertEqual(code, 2)
        self.assertIn("clean control failed", report["detail"])
        self.assertEqual(report["candidates"], [])

    def test_strict_ordering_and_opt_out(self):
        overlap = BOOKING.replace("    ctx.begin(0)\n", "    ctx.begin(0)\n    ctx.begin(1)\n", 1)
        code, report = self.explore(overlap)
        self.assertEqual(code, 2)
        self.assertIn("previous operation", report["detail"])
        self.out = self.root / "relaxed"
        code, report = self.explore("STRICT_ORDERING = False\n" + overlap)
        self.assertNotIn("previous operation", report.get("detail", ""))

    def test_priority_order_seeded_sampling_and_search_counts(self):
        source = """
import sqlite3
from contextlib import closing

def setup(ctx):
    with closing(sqlite3.connect(ctx.db)) as db:
        db.execute("CREATE TABLE t (x INTEGER)")

def run(ctx):
    ctx.begin(0)
    for n in range(10):
        ctx.marker(f"step-{n}")
    ctx.marker("commit-point")
    ctx.acknowledge(0)

def observe(ctx, history):
    pass
"""
        orders = []
        for name, seed in (("a", 1), ("b", 1), ("c", 2)):
            self.out = self.root / name
            code, report = self.explore(source, "--max-candidates", 4, "--seed", seed)
            self.assertEqual(code, 2)
            self.assertEqual(report["search"]["mode"], "sampled")
            self.assertEqual(report["search"]["generated"], 11)
            self.assertEqual(report["search"]["attempted"], 4)
            orders.append([c["recipe"]["index"] for c in report["candidates"]])
        self.assertEqual(orders[0][0], 10)  # the commit marker is prioritized
        self.assertEqual(orders[0], orders[1])
        self.assertNotEqual(orders[0], orders[2])

    def test_check_and_time_budgets(self):
        code, report = self.explore(self.atomic(), "--max-checks", 1)
        self.assertEqual(code, 2)
        self.assertEqual((report["budget_reason"], report["search"]["checked"]), ("check limit", 1))
        self.out = self.root / "timed"
        code, report = self.explore(self.atomic(), "--time-budget", 0.001)
        self.assertEqual((code, report["budget_reason"]), (2, "time budget"))

    def test_worker_timeout_kills_grandchildren(self):
        if not Path("/proc").is_dir():
            self.skipTest("needs /proc")
        pidfile = self.root / "grandchild.pid"
        source = BOOKING.replace("    ctx.begin(0)", f"""    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open({str(pidfile)!r}, "w").write(str(child.pid))
    time.sleep(30)
    ctx.begin(0)""")
        code, report = self.explore(source, "--worker-timeout", 1)
        self.assertEqual(code, 2)
        pid = int(pidfile.read_text())
        deadline = time.time() + 5
        while alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid))

    def test_early_exit_with_cut_status_is_a_harness_error(self):
        code, report = self.explore(BOOKING.replace("    ctx.begin(0)", "    import os\n    os._exit(77)\n    ctx.begin(0)"))
        self.assertEqual(code, 2)
        self.assertIn("without a result", report["detail"])

    def test_replay_by_candidate_runtime_and_old_bundles(self):
        code, report = self.explore(BOOKING)
        failing = [c for c in report["candidates"] if c["outcome"]["status"] == "verified-failure"]
        self.assertEqual(len(failing), 2)
        for item in failing:
            for wanted in (item["id"], item["id"].split("-")[0]):
                code, result = self.cli("replay", self.out, "--candidate", wanted)
                self.assertEqual((code, result["reproduced"], result["fingerprint"]),
                                 (1, True, item["fingerprint"]))
        self.assertEqual(self.cli("replay", self.out)[0], 2)  # two bundles: ambiguous
        self.assertEqual(self.cli("replay", self.out, "--candidate", "c_099")[0], 2)
        bundle = self.out / failing[0]["bundle"]
        meta_path = bundle / "bundle.json"
        original = json.loads(meta_path.read_text())

        old = json.loads(json.dumps(original))
        for key in ("runtime", "crashcheck", "config", "run_identity", "context"):
            old.pop(key)
        old["candidate"].pop("id")
        meta_path.write_text(json.dumps(old))
        self.assertEqual(self.cli("replay", bundle)[0], 1)

        mismatch = dict(original, runtime=dict(original["runtime"], sqlite="0.0.1"))
        meta_path.write_text(json.dumps(mismatch))
        code, result = self.cli("replay", bundle)
        self.assertEqual(code, 2)
        self.assertIn("runtime mismatch", result["detail"])
        self.assertEqual(self.cli("replay", bundle, "--ignore-runtime")[0], 1)

        meta_path.write_text(json.dumps(dict(original, version=99)))
        self.assertEqual(self.cli("replay", bundle)[0], 2)
        meta_path.write_text(json.dumps(original))
        (bundle / "scenario.py").write_text("# edited\n" + (bundle / "scenario.py").read_text())
        self.assertEqual(self.cli("replay", bundle)[0], 2)

    def test_bundle_with_a_link_is_rejected(self):
        self.explore(BOOKING)
        bundle = self.out / "failures" / "00000"
        (bundle / "raw" / "link").symlink_to(self.root)
        code, result = self.cli("replay", bundle)
        self.assertEqual(code, 2)
        self.assertIn("link", result["detail"])

    def test_cli_mode_validation(self):
        self.scenario.write_text(BOOKING, encoding="utf-8")
        cases = (["--mode", "modeled"], ["--mode", "process", "--model", "file-prefix-v1"],
                 ["--model", "file-prefix-v1"], ["--mode", "process", "--adapter", self.scenario])
        for n, extra in enumerate(cases):
            self.out = self.root / f"bad{n}"
            self.assertEqual(self.cli("test", self.scenario, "--out", self.out, *extra)[0], 2, extra)
        self.out = self.root / "alias"
        code, report = self.explore(BOOKING, command="run")
        self.assertEqual(code, 1)

    def modeled_adapter(self):
        adapter = self.root / "adapter.py"
        adapter.write_text("""
import shutil

def record(scenario, ctx, trace):
    scenario.run(ctx)
    (trace / "recorded").write_text("ok")

def recipes(trace, seed):
    yield {"step": 0, "extra": 2}

def reductions(trace, recipe):
    if recipe["extra"] > 0:
        yield {"step": 0, "extra": recipe["extra"] - 1}

def materialize(base, trace, recipe, dest):
    assert (trace / "recorded").read_text() == "ok" and recipe["extra"] >= 0
    shutil.copytree(base, dest, dirs_exist_ok=True)
    return {"cut": recipe["step"], "history": {"begun": [0], "acknowledged": [0]},
            "effects": {"app.sqlite": 2}}
""", encoding="utf-8")
        return adapter

    def test_modeled_bundle_regenerates_and_minimizes(self):
        code, report = self.explore(BOOKING, "--adapter", self.modeled_adapter())
        self.assertEqual((code, report["mode"]), (1, "modeled"))
        item = report["candidates"][0]
        self.assertEqual(item["effect_counts"], {"app.sqlite": 2})
        self.assertEqual(report["search"]["mode"], "adapter-ordered")
        bundle = self.out / item["bundle"]
        for name in ("base", "trace", "adapter.py", "bundle.json"):
            self.assertTrue((bundle / name).exists(), name)
        code, result = self.cli("replay", bundle, "--regenerate")
        self.assertEqual((code, result["reproduced"], result["regenerated"]), (1, True, True))

        shrunk = self.root / "shrunk"
        code, result = self.cli("minimize", bundle, "--out", shrunk)
        self.assertEqual(code, 1)
        self.assertTrue(result["minimized"])
        self.assertEqual(result["minimized_recipe"], {"step": 0, "extra": 0})
        self.assertEqual(json.loads((shrunk / "original" / "bundle.json").read_text())["candidate"]["recipe"]["extra"], 2)
        code, replayed = self.cli("replay", shrunk / "minimized", "--regenerate")
        self.assertEqual((code, replayed["reproduced"]), (1, True))

        (bundle / "trace" / "recorded").write_text("tampered")
        self.assertEqual(self.cli("replay", bundle, "--regenerate")[0], 2)

    def test_process_bundle_cannot_be_minimized(self):
        self.explore(BOOKING)
        code, result = self.cli("minimize", self.out / "failures" / "00000", "--out", self.root / "m")
        self.assertEqual(code, 2)
        self.assertIn("modeled", result["detail"])


if __name__ == "__main__":
    unittest.main()
