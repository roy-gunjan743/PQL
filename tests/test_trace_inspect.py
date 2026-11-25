"""Independent vectors for the read-only storage trace inspector."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from crashcheck.storage.inspect import summarize_trace
from crashcheck.storage.trace import TraceWriter


class TraceInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base"
        self.base.mkdir()
        (self.base / "app.sqlite").write_bytes(b"0000")
        self.trace = self.root / "trace"
        self.writer = TraceWriter(self.base, self.trace)

    def finish(self):
        self.writer.finish({"model": "file-prefix-v1"})

    def test_counts_prefixes_across_sync_and_receipt_boundaries(self):
        self.writer.append("begin", ordinal=0)
        self.writer.append("write", "app.sqlite", payload=b"A", offset=0)
        self.writer.append("sync", "app.sqlite", flags=2)
        self.writer.append("write", "app.sqlite", payload=b"B", offset=1)
        self.writer.append("marker", name="checkpoint")
        self.writer.append("ack", ordinal=0)
        self.finish()
        summary = summarize_trace(self.trace)
        self.assertEqual(summary["model"], "file-prefix-v1")
        self.assertEqual(summary["event_count"], 6)
        self.assertEqual(summary["crash_boundaries"], 6)
        self.assertEqual(summary["events_by_kind"],
                         {"ack": 1, "begin": 1, "marker": 1, "sync": 1, "write": 2})
        self.assertEqual(summary["effect_counts"], {"app.sqlite": 2})
        self.assertEqual(summary["durability_floors"], {"app.sqlite": 1})
        # begin:1, A:2, sync:1, B:2, marker:2, ack:2, all-kept:1
        self.assertEqual(summary["candidate_upper_bound"], 11)

        proc = subprocess.run(
            [sys.executable, "-m", "crashcheck", "inspect-trace", str(self.trace)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), summary)

    def test_inspection_rejects_corrupted_trace_payload(self):
        event = self.writer.append("write", "app.sqlite", payload=b"A", offset=0)
        self.finish()
        (self.trace / "blobs" / event["blob"]).write_bytes(b"X")
        proc = subprocess.run(
            [sys.executable, "-m", "crashcheck", "inspect-trace", str(self.trace)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["status"], "inconclusive")

    def test_empty_trace_has_zero_candidates(self):
        self.finish()
        summary = summarize_trace(self.trace)
        self.assertEqual(summary["candidate_upper_bound"], 0)
        self.assertEqual(summary["crash_boundaries"], 0)


if __name__ == "__main__":
    unittest.main()
