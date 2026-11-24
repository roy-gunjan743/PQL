import argparse
import json
import math
from pathlib import Path

from .engine import WorkerError, replay, run
from .evidence import inconclusive


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a positive finite number")
    return number


def main():
    parser = argparse.ArgumentParser(prog="crashcheck", description="Check SQLite application crash recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("test", "run"):
        execute = commands.add_parser(name, help="explore process cuts or modeled storage candidates")
        execute.add_argument("scenario", type=Path)
        execute.add_argument("--out", type=Path, required=True, help="new run directory")
        execute.add_argument("--seed", type=int, default=0)
        execute.add_argument("--max-candidates", type=positive, default=100)
        execute.add_argument("--max-generated", type=positive)
        execute.add_argument("--wall-seconds", type=seconds)
        execute.add_argument("--mode", choices=("process", "modeled"), default="process")
        execute.add_argument("--model")
        execute.add_argument("--adapter", type=Path, help="storage recorder and model adapter")
        execute.add_argument("--worker-timeout", type=seconds, default=30.0, metavar="SECONDS")
    reproduce = commands.add_parser("replay", help="verify a saved failure bundle in a fresh worker")
    reproduce.add_argument("bundle", type=Path)
    reproduce.add_argument("--candidate")
    reproduce.add_argument("--worker-timeout", type=seconds, default=30.0, metavar="SECONDS")
    args = parser.parse_args()
    try:
        if args.command in ("test", "run"):
            if args.mode == "modeled" and not args.adapter:
                raise ValueError("modeled mode requires --adapter")
            if args.model and args.model != "file-prefix-v1":
                raise ValueError("unsupported storage model")
            result = run(args.scenario, args.out.resolve(), args.seed, args.max_candidates,
                         args.worker_timeout, args.adapter, args.max_generated, args.wall_seconds)
            result = {k: v for k, v in result.items() if k != "candidates"}
            result["manifest"] = str(args.out / "manifest.json")
        else:
            result = replay(args.bundle.resolve(), args.worker_timeout, args.candidate)
    except (OSError, ValueError, TypeError, KeyError, WorkerError) as exc:
        result = inconclusive(str(exc))
    print(json.dumps(result, sort_keys=True))
    return {"pass": 0, "verified-failure": 1, "inconclusive": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
