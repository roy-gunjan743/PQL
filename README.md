# CrashCheck

CrashCheck runs a SQLite application from a clean fixture, stops the process at a scenario marker and checks what a fresh process can recover. It requires Python 3.11+ and uses the standard library.

## Run the booking example

From the checkout root, run the following commands. Choose a new output directory for each run.

```sh
python -m crashcheck run examples/booking.py --out booking-run
python -m crashcheck replay booking-run/failures/00000
python -m unittest discover -s tests -v
```

The example intentionally commits a seat decrement before inserting its booking. The first cut leaves a missing seat, reports `booking.capacity` and exits 1. Set `SPLIT_COMMIT = False` in a copy of the example to exercise the atomic control. All three cuts should pass.

## Scenario API

Supply one trusted, self-contained Python file. Workers run with their private fixture as the working directory. Use files under `ctx.root`, the SQLite path `ctx.db`, and the seeded generator `ctx.random`. Keep the scenario deterministic and close database connections in setup. Module globals and the generator start fresh in each worker.

| Hook | Responsibility |
| --- | --- |
| `setup(ctx)` | Create the base fixture, including `app.sqlite`. |
| `run(ctx)` | Perform operations and call `ctx.marker(name)` at interesting crash points. |
| `recover(ctx)` (optional) | Run application recovery after SQLite opens the candidate and passes its integrity check. |
| `observe(ctx, history)` | Check application state against `history.begun` and `history.acknowledged`. Return normally to pass or raise `crashcheck.CheckFailure(check_id, failure_class, message)` for a verified violation. |

Call `ctx.begin(ordinal)` before each operation and `ctx.acknowledge(ordinal)` when its receipt becomes externally visible. Ordinals are unique nonnegative integers. Check failures from the observer are application findings. Unexpected exceptions, missing databases and worker timeouts produce an inconclusive result.

## Exploration and evidence

Process mode discovers markers with an uninterrupted run, then repeats the workload on a fresh base clone for each selected marker in encounter order. Repeated names are valid. A cumulative digest checks the marker and operation-history prefix at each cut. The selected marker writes its evidence and calls `os._exit`, skipping Python cleanup. Recovery runs on a copy so opening SQLite cannot change the saved candidate or its hot journal. Process mode exercises process termination while the host OS stays running.

`--seed N` defaults to 0. `--max-candidates N` defaults to 100 and bounds attempted cuts, including duplicate states. `--worker-timeout SECONDS` defaults to 30 per invocation. Run directories contain `manifest.json` with the environment, input hashes, seed, budget, candidate order, outcomes, counts and reused verdicts. A SHA-256 fingerprint covers relative paths, file lengths and contents, including sidecars and empty directories. Verdict reuse also requires identical begun and acknowledged histories within the same run.

| Exit | Meaning |
| --- | --- |
| 0 | Every planned candidate passed. |
| 1 | At least one verified failure. Replay returns 1 for a reproduced failure. |
| 2 | Inconclusive or invalid invocation. Examples include zero cuts, a truncated clean run, worker errors, or inconsistent replay evidence. |

The CLI prints a JSON summary on standard output. A verified failure takes precedence over a partial exploration. Each new failure bundle stores the untouched raw fixture, scenario source, seed, cut or model recipe, history, fingerprints and expected check ID and failure class. Replay verifies the raw and scenario hashes and repeats recovery on a private copy. Only replay bundles whose scenario code you trust.

## Storage adapter contract

Pass `--adapter path/to/adapter.py` to explore modeled storage candidates. The storage engineer supplies this module and its SQLite recorder and materializer. The backend calls these hooks in worker processes and uses the same verifier, reports and bundles as process mode.

| Hook | Responsibility |
| --- | --- |
| `record(scenario, ctx, trace)` | Record the scenario workload on a private clone. Store the trace and blobs in the run's trace directory. Reject unsupported settings and invalid captures with an exception. |
| `recipes(trace, seed)` | Yield JSON-serializable model recipes in deterministic order. The scheduler reads up to the candidate limit plus one item to detect truncation. |
| `materialize(base, trace, recipe, dest)` | Treat base and trace as read-only. Write the complete raw file set to the empty destination. Return a mapping with `cut` and `history`, as below. |

```json
{"cut": 7, "history": {"begun": [0, 1], "acknowledged": [0]}}
```

The backend computes the fingerprint from the files it actually verifies. The adapter test uses a small synthetic recipe to exercise the contract. Storage-model qualification and independent QA oracles belong to their respective modules.
