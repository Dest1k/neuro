# Durable bounded-search recovery

Iteration 8 connects a native durable journal to the bounded CPU/CUDA-capable
host. It preserves consumed mining attempts and submission outcomes across
process restarts. Transport remains the existing loopback-only client: this
is not a production pool client or sustained mining result. The genuine proof
builder and separate official verifier remain unchanged.

## Run the recovery gate

After the real proof/native build in `docs/development.md`:

```bash
python tools/replay_recovery.py --output artifacts/search-recovery.json
```

The default client is `build/proof-cmake/neuro_pool_replay_client`; the default
separate verifier comes from the pinned proof build. Override paths with
`--native PATH` and `--reference PATH`. The Windows `run-cpu` action invokes
this gate directly on the local Windows machine, without Actions. Iteration-8
Windows execution remains pending; a recipe is not a passing observation.

The gate starts real native processes and a local TCP peer. Each scenario keeps
the listener/port and journal across processes. Every received proof is matched
to independent attempt/header/tile expectations and then checked by the separate
official Pearl executable. Client event labels cannot replace those checks.
Each complete test run creates a separate directory.

The native client adds `--journal PATH`; its parent must already exist. The
persisted peer scope is `loopback:PORT`, so recovery needs the same listener
port and journal. A different scope is refused. A production transport will
need an explicit stable pool/session identity; an ephemeral port is not that
identity. Omitting `--journal` retains the historical in-memory behavior used
by the existing 19-scenario replay, without durable recovery claims.

## Identity and progress

Records preserve the complete job ID, header, unscaled target, certificate
version, matrix shape and original transport encoding. They also contain the
exact root seed, `neuro/zero-b/row-seed/v1` derivation policy, pinned attempt
limit, connection/generation provenance, consumed prefix and attempt outcomes.
A send intent includes the exact connection epoch and request ID. Each new
connection receives a durably allocated monotonically increasing epoch.

The consumed search identity includes job ID, header, target, certificate,
shape and root seed. Ephemeral connection/generation and **gzip negotiation**
do not reset it. Gzip changes the encoding of the same raw proof; resetting the
search on negotiation changes could retransmit an uncertain share. Encoding is
preserved in binding provenance. The live coordinator still compares the full
current Job, including encoding and connection, when invalidating stale results.

Duplicate notify messages preserve progress. Changed header/target work has its
own plan even if its textual job ID is reused. Returning from A to B and back
to A resumes A's prior counter. Old identities are retained without eviction.
The same work cannot change its pinned attempt limit to reset progress.
A deliberately different root seed defines different search work; ordinary
recovery must retain the original seed.

## Ordering around process death

| Durable state at interruption | Recovery action |
|---|---|
| No consumed record for the next attempt | That index remains available after fresh authorization and notify |
| `running`: index consumed before preparation/computation | Mark `interrupted`; continue at the following index |
| `found`: verified proof exists, no send intent | Mark `interrupted`; discard that unsent result and continue |
| `intent`: persisted before the first socket send call | Mark `uncertain`; never automatically resend that attempt |
| `accepted` or `rejected`: matching response persisted | Retain the terminal result and continue |
| `miss`, `cancelled`, `error` or `discarded` | Retain the terminal outcome; do not repeat the attempt |

This deliberately sacrifices some potentially useful work for conservative
recovery. A crash after intent but before any socket bytes still means
`uncertain`. An ACK received just before its durable write fails cannot be
claimed as accepted after restart. There is no implemented pool reconciliation
protocol that can settle those gaps.

Later cumulative A-row state is reconstructed from the original seed and
attempt index. Skipping interrupted arithmetic does not omit its row mutation
from subsequent attempts. The independent later-attempt vectors still apply.

Each new process/connection must authorize and receive fresh work. Loading a
saved job alone never starts arithmetic or sends an old proof. The coordinator's
`restore_progress` initializes only a freshly published, unused generation. It
restores a consumed prefix, never a saved result or permission to submit it.
Verified-result sealing, stale-generation rejection and single-use commit remain
active. Persistence exceptions are fatal, outside ordinary network retry logic.

## Storage boundary

The append-only binary journal binds each frame's length, sequence, predecessor
and payload with domain-separated BLAKE3. Loading checks the complete chain
and legal state transitions. Unknown formats, torn tails, existing empty files,
corruption, changed peer scope and exceeded bounds are refused. Damaged history
is never silently truncated: a removed suffix could contain a send intent.

Successful mutations flush before returning: POSIX file `fsync`, parent
directory `fsync` on creation, or Windows `FlushFileBuffers`. A lifetime OS lock
excludes another cooperative writer and is released by process death. A failed
append poisons the writer. Reports remain available for diagnostics, but no
further writes are allowed. A report path or its temporary path aliasing the
journal is refused before journal open or TCP connection.

Limits are 64 retained identities, 64 attempts per identity, 32,768 frames and
4 MiB. Capacity stops work instead of forgetting prior sends. This is finite
recovery groundwork, not journal rotation for an indefinitely running miner.
Runtime journals belong in generated private storage such as `artifacts/`;
real job/seed state must not enter public source checkpoints.

Observed tests cover process termination on Linux. They do not prove recovery
from power loss, Windows storage behavior or an arbitrary network filesystem.
Use a local filesystem and storage that honors flushes. Cooperative locking
and a hash chain cannot prevent deliberate whole-file deletion, rollback to an
older internally valid file or uncooperative writes. Preserve the same journal
and seed rather than resetting history to make a refused action run.

## Evidence

Native reports retain per-process counters and add separate persistent
`journal` work/outcome counts. A later accepted attempt never erases an earlier
unknown. `completed:true` means this process finished its finite requested work;
it does not resolve every historical unknown or establish live pool acceptance.

The recovery gate covers abrupt stops before compute, after proof, after durable
send intent and after durable ACK. Another case kills the client only after the
peer receives and independently verifies a proof without sending its ACK. Other
cases cover rejection, later wins, A/B/A work changes, exhaustion, gzip-to-plain
recovery, corrupt/torn files, output aliasing and concurrent writers.

Explicit `--crash-point` and `--crash-attempt-index` fault injection requires
a journal and exists only in the bounded loopback client. It emits the exact
point/index/job event and exits with code 86 without destructors or a normal
report. These are validation hooks, not production recovery actions.

`reports/iteration-08-validation.json` and `docs/iterations/08.md` record observed
counts and unrun gates. The first physical GPU run still needs the owner's exact
SRBMiner stop/visible-batch restoration from `docs/gpu-miner-handoff.md`. These
CPU recovery checks do not stop the owner's miner.
