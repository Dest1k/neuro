# Bounded reproducible attempt search

Iteration 7 extends the existing zero-B preparation and host lifecycle to a
finite sequence of attempts within one job generation. The reference CPPminer
checkout remains pinned and unmodified. The root README remains empty.

## Signal and seed contract

The pinned donor's `src/cpu/cp_cpu_worker.cpp::zero_b_prepare_attempt` overwrites
one random A row per attempt and retains all other rows from earlier attempts.
Its production worker obtains each row seed from its CSPRNG. Our bounded search
uses an explicitly reproducible policy instead:

- The root seed is exactly 32 bytes; attempt 0 uses it unchanged.
- For index `i` in 1..63, the row seed is unkeyed BLAKE3 of the concatenation:
  ASCII `neuro/zero-b/row-seed/v1` without a NUL terminator, the 32 root-seed bytes,
  and `i` encoded as four little-endian bytes.
- Signal A and signal Bt initially contain zeroes. For attempt `i`, apply the
  donor's genuine `pearl_fill_random_a_row` for indices 0..i in order. Row
  collisions overwrite the earlier contents of that row; all other rows survive.
  Signal Bt stays zero.
- Commit the resulting signals, derive the existing certificate-specific noise
  seeds and prepare noisy A/Bt using the real donor functions.

`pearl::Request::attempt_index` selects an index in 0..63. Existing callers
continue to select index 0 by default. All historical first-attempt byte and
arithmetic fixtures remain unchanged. `PreparedInput` records the row-seed bytes
and complete mutation-row sequence for diagnosis.

This implementation reconstructs the cumulative signal from its root seed for
each attempt. It deliberately favors a small reproducible state over incremental
hashing, cached B panels or overlapping GPU work. Preparation is bounded by 64
row writes and the existing matrix dimensions. No throughput claim is made.
The fixed replay root seed is synthetic; it is not a live-mining entropy policy.

## Immutable work and attempt identity

`Coordinator::publish` still owns an immutable complete job snapshot: connection,
job ID, all header bytes, target, certificate version, shape and encoding. Exact
duplicates retain the generation and do not reset its search sequence.

`run_attempt(job_ticket, root_seed, index, compute, checkpoint)` requires indices
starting at zero and increasing by one. The first claimed attempt pins the root
seed for that generation. A skipped/repeated index, a changed root seed, an
out-of-range index or another in-flight attempt returns an error before another
arithmetic call. An invalid pre-claim request does not consume the next index.
A claimed index remains consumed after a computation/proof error.

Execution tickets own an immutable `AttemptIdentity` containing the root seed
and index. Preparation reads this captured seed, not the caller's later mutable
variable. The verified-result seal binds the share to this owned identity and
the full job ticket. Changing a public result's root seed, index or generation
cannot turn it into another submit permission.

Each attempt can invoke its bounded sink once, including a failed or throwing
sink. Different verified attempts of the same job can each be submitted.
Publication, invalidation and handoff remain serialized. A replacement job or
connection invalidates earlier attempts at every existing checkpoint and starts
its own sequence at zero. A backend exception remains an error even if the job
was concurrently invalidated; the TCP client stops rather than reusing that
failed arithmetic worker.

The guarantee is local to one Coordinator lifetime. The root seed, policy,
attempt indices, work identities and outcomes are recorded so the computation
can be reproduced. This is not a durable cross-process share-submission journal
or automatic recovery of a previously sent but unacknowledged share.

## Bounded TCP execution

The loopback client's default remains one attempt per generation. New options:

| Option | Contract |
|---|---|
| `--max-attempts N` | 1..64 attempts per generation; default 1 |
| `--stop-on-attempt-limit 1` | Explicitly finish with `reason: attempt_limit` after the sequence is exhausted and no response remains pending |
| `--pause-attempt-index I` | Select the attempt for a deliberate cancellation checkpoint; requires the existing pause stage/job options |

There is one arithmetic worker and at most one pending submission. A miss
advances to the next index. After a found proof, the client processes its exact
ACK/rejection before starting the next attempt. Existing response timeouts,
connection limits and the overall wall-clock deadline still bound the run.
Without the explicit stop option, an exhausted generation waits for new work
within that same deadline. This preserves the earlier rejection/replacement
replay behavior.

Reports include the root seed, derivation policy, per-generation attempt cap,
started/finished attempt counts and generation/index identity on proof and
response events. `attempt_limit` means the requested finite search finished;
it is not evidence of a discovered or accepted share. Accepted/rejected/lost
responses remain separate counters.

## Independent evidence

`tools/generate_attempt_fixtures.py` calls no native implementation or verifier.
It uses the pinned Python BLAKE3 binding, the existing independently implemented
signal/noise formulas and scalar integer matrix arithmetic. It preserves the
historical attempt-0 fingerprints and produces `attempt_manifest.json` and
`attempt_golden.hpp` under `native/fixtures/`. Checking is the default; `--write`
is an explicit fixture update.

The fixed corpus contains attempts 0..7, the first repeated-row case at index 24,
the final allowed index 63 and attempt 0 after changing header byte 75. It checks
row seeds, all mutation rows, complete signal/noisy-matrix hashes, commitments,
noise seeds, every transcript word and every final matrix cell through their
independently generated fingerprints.

A compact synthetic target is deliberately selected from the independent
jackpots to produce seven actual misses followed by a real winner at attempt 7.
That selected fixture demonstrates continuation and exact correctness; its
success frequency is not an estimate for network difficulty or mining revenue.

The native `neuro_attempt_search` test additionally exercises duplicate work,
root-seed capture, skipped/repeated indices, per-attempt handoff, forged identity,
byte-identical reproduction, cancellation during the second attempt, a new-job
restart, concurrent-call exclusion and failure of one attempt's sink.

Four additional actual TCP scenarios complement the previous 15:

| Scenario | Observation required |
|---|---|
| `multi_attempt` | Three distinct proof payloads and signal commitments under one generation, each independently verified |
| `miss_then_found` | Seven full-tile misses followed by attempt 7's independently predicted proof |
| `attempt_limit` | Three real misses, zero submissions and explicit bounded completion |
| `cancel_later_attempt` | First proof acknowledged, second attempt cancelled after arithmetic, replacement job restarts at zero |

The peer obtains expected attempt identities from its own issued-job ledger and
independent fixture. It compares the actual proof's authenticated Merkle roots
and selected rows with that expected attempt before invoking the separately
pinned official verifier. A native event claiming a later index cannot relabel
an earlier valid proof.

## Commands and limits

After the genuine Rust proof build and CMake proof configuration:

```sh
cmake --build build/proof-cmake --parallel 4
ctest --test-dir build/proof-cmake --output-on-failure
python tools/replay_pool.py --output artifacts/pool-replay.json
python tools/generate_attempt_fixtures.py --check
```

Fixture checking requires the existing hash-locked Python fixture environment;
it is separate from the runtime miner dependency set. The Windows entrypoint
runs the same native and TCP gates. The CUDA-linked host uses the same attempt
and proof coordination with its existing CUDA compute callback.

Actual GPU execution, owner-host SSH, sustained mining, large-panel operation,
performance optimization and live-pool acceptance each need their own observed
results. The attempt implementation does not replace those future checks.
