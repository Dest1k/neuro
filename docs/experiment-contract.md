# Experiment contract (iteration 1)

`tools/neuroctl/experiments.py` is a local command runner and comparison gate.
It does not run an LLM, manage SSH, supply a GPU kernel, or establish that a miner
is profitable. These are deliberately separate from measurement.

## API

```python
from neuroctl.experiments import run_command, write_result, evaluate

verification = run_command(["path/to/verifier", "vectors.json"],
                           stage="verification", timeout_s=60)
measurement = run_command(["path/to/benchmark", "--repeat", "1"],
                          stage="measurement", timeout_s=60)
write_result("results/process.json", measurement)
verdict = evaluate(candidate, baseline)
write_result("results/comparison.json", verdict)
```

Commands are argument arrays and use `shell=False`. The runner drains both output
streams while retaining only their last 64 KiB each by default. It records a
unique UUID, start time, elapsed wall time, return code, launch errors, output
truncation, and timeout. JSON writes use a temporary file and atomic replacement;
NaN and infinity cannot be saved. Existing run IDs are preserved when saving the
same observation. A comparison gets its own run ID.

Each command must be a finite local stage, not a service or daemon. POSIX uses a
new session and kills the owned process group on timeout and after leader exit.
Windows uses a new process group and `taskkill /T /F` on timeout. Windows tree
cleanup is best effort, especially if the leader exits before its descendants.
Detached children, a hung driver, and a hung operating system are outside the
runner's recovery guarantee. Incomplete output or an unreaped process fails the
gate. The timeout is the command deadline; teardown can take additional seconds.
Windows behavior must be tested on the target laptop before unattended GPU runs.

## Evidence schema

Each side of a comparison is a `neuro.experiment.v1` object with:

- `kind`: `cpu_smoke`, `gpu_kernel`, or `pool`.
- `synthetic`: explicitly `false` for observations eligible for evaluation.
  The included example is explicitly synthetic and **must fail** the gate.
- `identity`: nonempty `hardware`, `workload`, `profile`, `environment`,
  `device_kind`, and an explicit `power_limit_w`. CPU smoke uses `device_kind:
  "cpu"`; its power limit may be null. GPU/pool evidence uses `"cuda"` and a
  positive power limit. Use a physical device identifier in `hardware`, the
  dataset/protocol/shape in `workload`, clocks and operational settings in
  `profile`, and OS/driver/toolchain versions in `environment`. Do not put the
  candidate implementation commit in `environment`: it is what changes.
- `correctness`: `exact` must be the boolean `true`, `verifier_id` identifies the
  independent reference implementation/version, and `verification` is its
  successful observed `run_command(..., stage="verification")` record.
- `samples`: at least three independent measurements, each with a positive finite
  `elapsed_s` and a successful `measurement` process record. Each sample needs a
  different run ID across both compared experiments and must not reuse the
  verification run. `elapsed_s` is the
  measured operation interval; if kernel events are used, it is not process
  startup wall time. Record the interval's definition in the workload/profile.

For `pool`, identity additionally contains `pool` and `difficulty_policy`.
Every sample additionally supplies integer `accepted_shares > 0`, integer
`invalid_shares == 0`, and positive `accepted_difficulty` (the sum of difficulties
of accepted shares). The interval is wall time, including miner stalls and stale
work. Pool work must come from captured confirmations, not a displayed miner
hashrate or a model's estimate. Vardiff difficulty must be recorded per accepted
share before summation; do not multiply share count by the latest difficulty.

The producer must actually run an independent verifier and parse its result.
`exact: true` is necessary but insufficient; a text field saying `PASS`, missing
verification, nonzero return code, timeout, or incomplete capture fails the gate.
An exit code of zero alone does not establish mathematical correctness. This
module validates records, **not their authenticity**: fabricated JSON can still
lie. Keep raw observations and their provenance; never let an LLM fill measured
fields from expectations. The evaluator does not attest GPU presence.

## Comparison rule

Kinds and all declared comparison identity fields must match. First the gate
checks correctness and process evidence; only then it compares medians.

- CPU/GPU measurements compare elapsed seconds (lower is better).
- Pool measurements compare accepted difficulty per wall second (higher is
  better). Raw reported TH/s is ignored.
- Each side's `(maximum - minimum) / median` must be at most 0.15 by default.
- A regression greater than 2% fails by default. Both tolerances are explicit API
  parameters; changing them must be a documented experiment decision.

`passed` means the supplied evidence passes these gates and the non-regression
threshold. It does not mean a statistically established speedup, an optimal GPU
kernel, profitability, or superiority on a different card. A valid CPU smoke
comparison remains `cpu_smoke_only`. Pool observations are stochastic: collect
enough accepted work to assess uncertainty before making a small speedup claim;
the gate's spread check is not a confidence interval. A complete future benchmark
producer must also record rejected/stale shares and explain their classifications.

The tests use synthetic fixtures for schema checks and real local Python
subprocesses for runner checks. They do not mine Pearl or benchmark a GPU.
