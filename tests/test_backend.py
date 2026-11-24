import json
import subprocess
import sys
import tempfile
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
        item = next(c for c in report["candidates"]
                    if c["outcome"].get("check_id") == "booking.capacity")
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
        source = source.replace("    if remaining", "    if not history.acknowledged:\n        assert not (ctx.root / 'cleaned').exists()\n    if remaining")
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
    with ctx.connect() as db:
        db.execute("UPDATE seats SET remaining = 0")
        db.execute("INSERT INTO bookings VALUES (0)")
""" + "\ndef observe(" + BOOKING.split("def observe(")[1]
        code, report = self.explore(source)
        self.assertEqual((code, report["unique_states"], report["verifications"]), (1, 1, 2))
        self.assertEqual(report["deduplicated"], 1)
        self.assertEqual(sum("reused_from" in c for c in report["candidates"]), 1)
        self.assertTrue(any(c["outcome"].get("check_id") == "booking.receipt"
                            for c in report["candidates"]))

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

    def test_strict_order_and_opt_out(self):
        source = BOOKING.replace("    ctx.begin(0)", "    ctx.begin(0)\n    ctx.begin(1)")
        code, report = self.explore(source)
        self.assertEqual(code, 2)
        self.assertIn("previous operation", report["detail"])

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
        self.assertTrue((self.out / item["bundle"] / "trace").is_dir())

    def test_replay_candidate_and_typed_stages(self):
        _, report = self.explore(BOOKING)
        item = next(c for c in report["candidates"] if c["outcome"]["status"] == "verified-failure")
        bundle = self.out / item["bundle"]
        self.assertEqual(self.cli("replay", bundle, "--candidate", item["id"])[0], 1)
        self.assertEqual(self.cli("replay", bundle, "--candidate", "c_wrong")[0], 2)
        self.assertEqual(item["outcome"]["checks"][0]["stage"], "open")
        meta = json.loads((bundle / "bundle.json").read_text())
        meta["version"] = 99
        (bundle / "bundle.json").write_text(json.dumps(meta))
        self.assertEqual(self.cli("replay", bundle)[0], 2)

    def test_recovery_error_and_observer_mutation(self):
        source = BOOKING + "\ndef recover(ctx):\n    raise RuntimeError('recovery failed')\n"
        code, report = self.explore(source)
        self.assertEqual(code, 2)
        self.assertEqual(report["control"]["checks"][-1]["stage"], "app-recovery")
        self.out = self.root / "mutation"
        source = BOOKING + """
original_observe = observe
def observe(ctx, history):
    with ctx.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS observer_mutation (x)")
    original_observe(ctx, history)
"""
        code, report = self.explore(source)
        self.assertEqual(code, 2)
        self.assertEqual(report["control"]["checks"][-1]["check_id"], "observe.readonly")

    def test_foreign_key_violation(self):
        source = """
check_foreign_keys = True
import sqlite3
def setup(ctx):
    with sqlite3.connect(ctx.db) as db:
        db.executescript("CREATE TABLE p(id INTEGER PRIMARY KEY); CREATE TABLE c(p INTEGER REFERENCES p(id));")
def run(ctx):
    with ctx.connect() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO c VALUES (42)")
    ctx.marker("after-write")
def observe(ctx, history):
    pass
"""
        code, report = self.explore(source)
        self.assertEqual(code, 2)
        self.assertEqual(report["control"]["check_id"], "sqlite.foreign_keys")

    def test_sampled_plan_is_reproducible(self):
        source = BOOKING.replace('ctx.marker("after-acknowledgement")',
                                 ('\n    ').join(f'ctx.marker("after-acknowledgement-{i}")'
                                           for i in range(12)))
        _, first = self.explore(source, "--max-candidates", 3, "--max-generated", 20)
        self.assertEqual(first["plan"], "sampled")
        ids = [c["id"] for c in first["candidates"]]
        self.out = self.root / "second"
        _, second = self.explore(source, "--max-candidates", 3, "--max-generated", 20)
        self.assertEqual(ids, [c["id"] for c in second["candidates"]])


if __name__ == "__main__":
    unittest.main()
