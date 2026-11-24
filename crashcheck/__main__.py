import argparse
import json
import math
from pathlib import Path

from .engine import WorkerError, minimize, replay, run
from .evidence import inconclusive

MODELS = Path(__file__).resolve().parent / "models"


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


def resolve_model(name):
    """Map a model name such as file-prefix-v1 to its adapter module."""
    path = MODELS / f"{name.replace('-', '_')}.py"
    if not path.is_file():
        raise ValueError(f"storage model {name!r} is not installed")
    return path


def select_adapter(mode, adapter, model):
    if adapter and model:
        raise ValueError("pass either --adapter or --model, not both")
    if mode == "process" and (adapter or model):
        raise ValueError("--mode process takes no adapter or model")
    if mode == "modeled" and not (adapter or model):
        raise ValueError("--mode modeled needs --model or --adapter")
    return resolve_model(model) if model else adapter


def add_run_options(cmd, test=False):
    cmd.add_argument("scenario", type=Path)
    cmd.add_argument("--out", type=Path, required=True, help="new run directory")
    cmd.add_argument("--seed", type=int, default=0)
    cmd.add_argument("--max-candidates", type=positive, default=100,
                     help="cap on attempted candidates (generation budget)")
    cmd.add_argument("--max-checks", type=positive, default=None,
                     help="cap on fresh verifications (check budget)")
    cmd.add_argument("--time-budget", type=seconds, default=None, metavar="SECONDS",
                     help="wall-clock budget for the whole run")
    cmd.add_argument("--adapter", type=Path, help="storage recorder and model adapter")
    cmd.add_argument("--mode", choices=("process", "modeled"),
                     help="default: modeled when an adapter or model is given, else process")
    cmd.add_argument("--model", help="installed storage model, for example file-prefix-v1")
    cmd.add_argument("--worker-timeout", type=seconds, default=30.0, metavar="SECONDS")


def main():
    parser = argparse.ArgumentParser(prog="crashcheck", description="Check SQLite application crash recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    add_run_options(commands.add_parser("test", help="explore marked process cuts or modeled storage candidates"))
    add_run_options(commands.add_parser("run", help="alias of test"))
    reproduce = commands.add_parser("replay", help="verify a saved failure in a fresh worker")
    reproduce.add_argument("bundle", type=Path, help="failure bundle, or a run directory with --candidate")
    reproduce.add_argument("--candidate", help="candidate ID such as c_017")
    reproduce.add_argument("--regenerate", action="store_true",
                           help="rebuild a modeled candidate from base, trace and recipe first")
    reproduce.add_argument("--ignore-runtime", action="store_true", help="skip the SQLite version check")
    reproduce.add_argument("--worker-timeout", type=seconds, default=30.0, metavar="SECONDS")
    shrink = commands.add_parser("minimize", help="shrink a modeled failure through adapter reductions")
    shrink.add_argument("bundle", type=Path)
    shrink.add_argument("--out", type=Path, required=True, help="new directory for original and minimized")
    shrink.add_argument("--candidate")
    shrink.add_argument("--ignore-runtime", action="store_true")
    shrink.add_argument("--worker-timeout", type=seconds, default=30.0, metavar="SECONDS")
    args = parser.parse_args()
    try:
        if args.command in ("test", "run"):
            adapter = select_adapter(args.mode, args.adapter, args.model)
            result = run(args.scenario, args.out.resolve(), args.seed, args.max_candidates,
                         args.worker_timeout, adapter, args.max_checks, args.time_budget, args.model)
            result = {k: v for k, v in result.items() if k != "candidates"}
            result["manifest"] = str(args.out / "manifest.json")
        elif args.command == "replay":
            result = replay(args.bundle.resolve(), args.worker_timeout, args.candidate,
                            args.regenerate, args.ignore_runtime)
        else:
            result = minimize(args.bundle.resolve(), args.out.resolve(), args.worker_timeout,
                              args.candidate, args.ignore_runtime)
    except (OSError, ValueError, TypeError, KeyError, WorkerError) as exc:
        result = inconclusive(str(exc))
    print(json.dumps(result, sort_keys=True))
    return {"pass": 0, "verified-failure": 1, "inconclusive": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
