"""Capture a SQLite workload without weakening the SQLite durability profile."""

import os
import shutil
from dataclasses import asdict
from tempfile import TemporaryDirectory
from pathlib import Path

from crashcheck.evidence import fingerprint, inventory

from .trace import TraceWriter


def record(scenario, ctx, trace: Path):
    """APSW-based recording hook used by the existing backend worker.

    A temporary initial clone is retained only until a byte-exact all-kept
    replay has been checked. The backend already stores its own base fixture.
    """
    try:
        import apsw
        from .vfs import ConnectionBridge, TracingVFS
    except ImportError as exc:
        raise RuntimeError("file-prefix-v1 requires APSW: pip install apsw") from exc

    trace.mkdir(parents=True, exist_ok=True)
    initial = fingerprint(inventory(ctx.root))
    with TemporaryDirectory(prefix="crashcheck-storage-base-") as work:
        base = Path(work) / "base"
        shutil.copytree(ctx.root, base)
        writer = TraceWriter(ctx.root, trace)
        vfs = TracingVFS(writer, name=f"crashcheck-{os.getpid()}-{id(writer)}")
        original_begin, original_ack, original_marker = ctx.begin, ctx.acknowledge, ctx.marker

        def begin(ordinal):
            original_begin(ordinal)
            writer.append("begin", ordinal=ordinal)

        def acknowledge(ordinal):
            original_ack(ordinal)
            writer.append("ack", ordinal=ordinal)

        def marker(name):
            writer.append("marker", name=name)
            original_marker(name)

        ctx.begin, ctx.acknowledge, ctx.marker = begin, acknowledge, marker
        try:
            with ConnectionBridge(ctx, vfs.name):
                scenario.run(ctx)
        finally:
            ctx.begin, ctx.acknowledge, ctx.marker = original_begin, original_ack, original_marker
            vfs.unregister()
            writer.stream.close()

        writer.finish(dict(
            profile={"journal_mode": "delete", "synchronous": "extra", "mmap_size": 0,
                     "single_writer": True, "sqlite_version": apsw.sqlitelibversion(),
                     "apsw_version": apsw.apswversion()},
            baseline_fingerprint=initial,
            final_fingerprint=fingerprint(inventory(ctx.root)),
            final_history=asdict(ctx.history),
            model="file-prefix-v1",
            limitations="Per-file prefix persistence only; no torn sectors or directory crash model",
        ))

        # The all-kept recipe is a mandatory completeness check. It prevents
        # false confidence if some writes bypass our VFS or metadata events.
        from .model import materialize, all_kept_recipe
        with TemporaryDirectory(prefix="crashcheck-all-kept-") as tmp:
            dest = Path(tmp) / "materialized"
            dest.mkdir()
            all_kept = all_kept_recipe(trace)
            materialize(base, trace, all_kept, dest)
            observed = fingerprint(inventory(dest))
            if observed != fingerprint(inventory(ctx.root)):
                raise ValueError("all-kept replay differs from the completed workload: "
                                 "unsupported I/O or missing traced effects")
