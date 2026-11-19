# Job lifecycle and local pool replay

Iteration 5 connects the real Pearl input, full-K arithmetic, jackpot selection,
PlainProof generation, local verification and TCP submission paths. The native
client talks to a Python peer on `127.0.0.1`; the peer verifies the actual received
proof in a separate process using pinned official Pearl code. These are synthetic
jobs and a local pool implementation. No live pool shares or GPU execution are
implied by a passing CPU replay.

Iteration 6 adds native Windows build/run entry points and a bounded SSH worker
for this same lifecycle. The historical observations in this document are the
iteration-5 Linux runs. The iteration-6 Windows CPU/proof/loopback gates passed
on an actual GitHub Windows runner; its separate evidence is recorded in
`reports/iteration-06-validation.json`. Physical GPU execution remains unobserved.

Iteration 7 adds bounded cumulative attempts and four additional real TCP
scenarios. `docs/attempt-search.md` specifies the seed policy, per-attempt
identity, independent expected results and continuation/limit behavior.

Iteration 8 adds optional durable search recovery and a separate fresh-process
TCP gate. `docs/search-recovery.md` records the journal contract and conservative
handling of interrupted/uncertain attempts. Existing no-journal replay remains
available and does not itself claim persistence. Current checks run directly
locally or on owner hardware; GitHub Actions remains disabled.

## Source and reuse boundary

The wire dialect comes from CPPminer commit
`5b68506af5e6433ee5c25aa9e6fbb00b537590aa`, particularly
[`cp_pool.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/common/cp_pool.cpp),
[`cp_pool_notify.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/common/cp_pool_notify.cpp),
`cp_json_frame.cpp` and `cp_tcp.cpp`. The adapter actually invokes its TCP
connection/framing, notify parser, authorize/submit serializers and Rust gzip
encoder. The existing asynchronous session/reader, fee switching and unrelated
coin paths are not started. No successful-looking transport or proof stubs are
linked. The pinned checkout stays unmodified.

This is the donor's LuckyPool/Kryptex dialect. The official Pearl gateway uses
different JSON-RPC methods (`getMiningInfo`, `submitPlainProof`); see its
[pinned schema](https://github.com/pearl-research-labs/pearl/blob/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c/miner/pearl-gateway/src/pearl_gateway/miner_rpc/schemas.py).
Official cryptographic verification does not establish equivalence of those
network protocols or compatibility with a current external pool.

## Immutable work and cancellation

`native/host/mining_job.cpp` owns a small `Coordinator`. Each published job has
an immutable snapshot: complete 76-byte header, job ID, unscaled 32-byte target,
certificate version, matrix shape, encoding flag and connection identity.
Any change creates a new monotonic generation. An exact duplicate preserves the
existing ticket; a malformed new job does not destroy valid current work.

This addresses a concrete limitation in the donor: its work key uses the ID and
only the first eight header bytes. A later header change, target change or
certificate change needs to invalidate an attempt even if that key is unchanged.
An old connection's late disconnect cannot cancel work from a new connection.

One worker owns its captured ticket throughout preparation, computation,
selection and proof generation. Seven stage checkpoints and per-tile checks
discard stale work. In-flight computation is allowed to finish using its own
buffers; cancellation never frees memory underneath a CUDA launch. A backend
exception remains an error even when the ticket concurrently becomes stale.
The client exits that worker process instead of reusing a potentially damaged
CUDA context.

Before proof generation, a selected winning tile's **32 transcript words** are
compared with independent CPU scalar arithmetic over the surrounding 16x16
region. This is stronger than checking only the final jackpot: a controlled
pair of corruptions can leave its folded value unchanged. Full-kernel fixture
checks remain necessary, because this selected-tile check does not validate
every losing tile or prove a claimed miss correct.

The real Rust verifier must accept the raw proof before a Share can reach the
sink. A private verified-result identity prevents public result-field edits
from turning an error, foreign result or modified proof into an accepted Share.
Generation validation and handoff share a mutex with publication. Each immutable
attempt ticket can invoke the sink at most once, including failed/throwing sends. The sink must
be bounded and must not reenter the coordinator.

Freshness is local to already processed notifications. New remote work or bytes
still in flight can legitimately cause the pool to reject an already sent share.
No local lock can revoke those bytes or guarantee that a remote job never changes.

## Supported wire profile

Authorization uses the actual donor serializer with these synthetic values:

```json
{"jsonrpc":"2.0","id":1,"method":"mining.authorize","params":{"wallet":"offline-wallet","worker":"replay-worker","agent":"neuro-replay","password":"x","type":"v2"}}
```

Only the matching response ID, `result:true` and an absent/null error authorize
work. A top-level `type:"v2"` enables gzip. The connection resets negotiation on
reconnect. Nested encoding decoys and authorize result-object variants do not
satisfy this explicit profile. No `mining.subscribe` is invented.

Notifications use `mining.notify` with an object containing `job_id`, a 152-digit
header hex string, explicit `cert_version` 2 or 3, and a 64-digit target hex string.
The donor's no-params/top-level object layout is also supported. Matrix dimensions
and attempt seed are local settings. Positional notify arrays and Bitcoin
`clean_jobs`/extranonce semantics are outside this Pearl profile.

`mining.set_difficulty` accepts a positive finite number or one-element array.
It supplies a missing target on a **subsequent** notify. It does not mutate the
active snapshot, and an explicitly malformed target never falls back silently.
The fallback follows the donor's `(0xFFFF << 208) / difficulty` conversion.

Submission is the donor's actual `mining.submit` with `job_id`, `plain_proof` and
`hs:0`. `plain_proof` is base64 of raw bincode or, after v2 negotiation, base64 of
its gzip encoding. The zero speed field avoids inventing performance evidence.
No network hashrate or earnings can be inferred from these replay timings.

The local guard validates JSON grammar and rejects duplicate keys before using
donor extractors. Incoming messages are limited to 65,535 bytes, 16 nesting
levels and 256 values. Strings use ASCII and ordinary JSON escapes; Unicode
escapes are not supported in this bounded profile. The framing tests include
fragmented and concatenated messages, while the guard catches malformed JSON
which balanced-bracket framing alone would accept.

## Real peer and independent checks

`tools/replay_pool.py` opens an ephemeral loopback TCP port and starts the real
native executable. It parses outgoing wire JSON independently with Python's
standard library. Its own issued-job ledger supplies the header, certificate
and target to `pearl-proof-check`; those expectations are never accepted from a
client event or the proof being checked.

For each submitted payload, it validates base64, bounds decompression to a single
gzip member if negotiated, and passes the decoded raw proof to the official
verifier pinned at `0dbdb2ffd8b81193a5079bb67a59aecc10ad501c`. Success requires exit
zero, `verified:true`, the expected verification stage and the correct source
pin. A crash, panic, argument error or timeout fails the replay.

The original 15 scenarios cover:

| Scenario | Observed condition |
|---|---|
| Gzip / plain success | cert3 and cert2 proofs, fragmented authorization, coalesced messages |
| Wrong response ID | unrelated success is ignored; explicit rejection then subsequent success |
| Stale response | valid proof deliberately rejected as stale; work continues on a fresh job |
| Duplicate notify | one generation and one attempt for identical work |
| Cancellation before compute | replaced job never reaches the compute callback |
| Cancellation after compute | same ID with changed header byte 75 invalidates the old result |
| Cancellation after proof | same ID/header with changed cert version invalidates the proof |
| Target change | same ID/header/cert with a changed target creates a new generation |
| Reconnect after verify | old proof is discarded; new connection renegotiates gzip to plain |
| Malformed notify recovery | bad header, absent ID and cert99 do not become executable work |
| Malformed frame recovery | client disconnects on `{]` and completes after reconnect |
| Rejected authorization | first connection is rejected; second authorizes normally |
| Lost acknowledgement | verified wire proof followed by EOF remains unacknowledged |
| Silent peer | connection stays open without an ACK; native response timeout records uncertainty |

The first table row is two separate scenarios. These original cases receive
17 actual proof submissions because each explicit-rejection scenario submits twice.
Iteration 7 adds `multi_attempt`, `miss_then_found`, `attempt_limit` and
`cancel_later_attempt`: the complete run now contains 19 scenarios and 23 wire
proofs. The peer also compares authenticated proof roots and selected rows with
independently expected attempt identities; a valid old proof cannot be relabelled
as a new attempt. Details and the deliberately selected miss-then-found target
are in `docs/attempt-search.md`.
Native stdout events coordinate deterministic checkpoints and explain ordering;
TCP payloads, the independent ledger and verifier establish proof correctness.
Checkpoint waits occur only in explicitly requested replay scenarios. They
release on actual generation invalidation, not a fake mining RPC.

`submissions`, `accepted`, `rejected` and `pending_lost` are distinct counters.
Only an exact pending request ID on its connection updates accepted/rejected.
Successful socket writing does not count as acceptance. The lost-ACK scenario
and the separate silent-peer scenario expect native exit 1, `completed:false`,
`pending_lost:1` and zero accepted or rejected shares. The silent peer additionally
requires the native `submit_response_timeout` event. The overall replay passes
because it observed that expected uncertainty. Its accepted count always refers
to this local peer, never a live pool.

## Reproduce the CPU replay

On Linux, after the real proof build in `docs/proof.md`:

```bash
cmake -S . -B build/proof-cmake -DCMAKE_BUILD_TYPE=Release -DNEURO_ENABLE_PROOF=ON
cmake --build build/proof-cmake -j 2
RAYON_NUM_THREADS=2 ctest --test-dir build/proof-cmake --output-on-failure
python tools/replay_pool.py --output artifacts/pool-replay.json
```

Proof mode now has 14 native tests on GCC/Linux. The replay additionally checks
the actual peer exchanges. Its default backend is CPU; `--scenario NAME` selects
individual cases. `--native` and `--reference` select explicit built executables.
Reports retain events, issued-job identities, payload fingerprints, real verifier
results and native counters. They contain no actual wallet or credentials.

On Windows, the fixed MSVC/Ninja actions build the native executables and the
separate official `.exe` checker, then run the same replay:

```powershell
./windows/ci.ps1 -Action build-cpu
./windows/ci.ps1 -Action run-cpu
```

The Python runners select `build/windows/` and the Windows Rust target directory
on that host. `run-cpu` also checks native CTest and the 66-case proof corpus;
its loopback report is `artifacts/windows/pool-replay.json`. The prior successful
build report and source/binary fingerprints must match before it starts.
Windows omits the three GCC/Linux donor arithmetic differential tests, so its
native test count is different: the iteration-6 Windows run passed 10 native
tests, all 66 proof checks and all 15 TCP scenarios with 17 independently
verified wire proofs. The validation report identifies the tested commit and
CI run. This observation does not establish a connection to the owner's laptop.

## CUDA integration and its present evidence

`native/cuda/mining_cuda.cu` implements the same Compute interface. It validates
the bounded input before any CUDA call, selects an SM120 device, owns a stream
and device buffers, uploads noisy A/Bt, invokes our actual full-K kernel, downloads
the transcript and synchronizes before returning. The final matrix is left empty;
jackpot selection uses the transcript. The normal host
generation/proof/submit checks apply to its result without a separate protocol.

Build after preparing the real Rust library:

```bash
cmake -S . -B build/host-cuda -DCMAKE_BUILD_TYPE=Release \
  -DNEURO_ENABLE_CUDA=ON -DNEURO_ENABLE_PROOF=ON \
  -DNEURO_BUILD_CPU_CHECKS=OFF -DCMAKE_CUDA_ARCHITECTURES=120
cmake --build build/host-cuda -j 2
ctest --test-dir build/host-cuda --output-on-failure
python tools/replay_pool.py --native build/host-cuda/neuro_pool_replay_client_cuda \
  --backend cpu --scenario gzip_happy --scenario plain_happy \
  --work-dir artifacts/pool-replay-cuda-linked --output artifacts/pool-replay-cuda-linked.json
```

The three CTest cases and the explicit CPU-backend replay do **not** initialize
or launch a GPU. They check host boundaries and the fully linked executable.
Iteration 5 records Linux CUDA 13.5.37/GCC14 compilation and linking for SM120.
Physical execution remains unobserved. On an actual suitable Linux GPU host,
after the earlier component correctness probes pass, the same runner supports:

```bash
python tools/replay_pool.py --native build/host-cuda/neuro_pool_replay_client_cuda \
  --backend cuda --device 0 --output artifacts/pool-replay-gpu.json
```

This is an executable future hardware check, not a claim that it ran here.
Windows uses the corresponding fixed action sequence:

```powershell
./windows/ci.ps1 -Action build-probes
./windows/ci.ps1 -Action run-cpu
./windows/ci.ps1 -Action run-cuda -Device 0
```

`build-probes` compiles the real proof library, CUDA probes and combined host.
`run-cuda` first requires successful CPU gates for matching source/binaries,
then checks the three physical GPU probes before the full replay with an
explicit CUDA backend. A successful Windows CPU job alone cannot satisfy it.
Prerequisites, output paths and bounded remote invocation are documented in
`docs/windows-worker.md`; no physical GPU result is claimed here.

## Bounded operation and remaining work

The replay client defaults to one deterministic zero-B attempt per generation;
`--max-attempts 1..64` enables a bounded cumulative sequence. It uses a fixed
synthetic root seed and small 128/256 dimensions. It is not a continuous
production entropy/search scheduler. The peer exercises 128x128 jobs. Real pool
configuration, production work scheduling and performance tuning remain separate
tasks. The donor connect used here receives only the numeric host `127.0.0.1`,
and that literal is checked again at the socket call. `assess_endpoint` classifies
every other host as external and does not resolve it. Gitignored
`configs/pool-endpoint.local.json` may name one exact endpoint; a named external
endpoint stays undialable, and the file is rejected when it carries a wallet,
worker, or password. Accepted shares remain unobserved. Iteration 6 implements local model-cost reservations and reconciliation
in `neuroctl budget`; actual DSH/provider dispatch and billing still need their
own integration and observations. See `docs/model-selection.md`.

The client has connection, submission, socket and scheduling limits. A native
deadline cannot preempt an in-flight GPU call or a CUDA destructor stuck in a
driver error. On Linux, the Python runner owns a separate process group and
enforces termination/kill deadlines; a process that ignores SIGTERM is tested
and reaped. On Windows, the fixed entry point and SSH worker wrap the action
in a Windows Job Object which owns its descendants and closes them on timeout
or cleanup. The inner Python `taskkill` fallback alone does not guarantee cleanup
after a process leader exits; the outer job supplies that ownership boundary.
Neither path can recover a completely hung operating system. No power/clock
changes, TDR changes or driver replacements are performed.

Historical Linux evidence: `reports/iteration-05-validation.json`, the native/CUDA
logs and `reports/iteration-05-pool-replay.json`. Current platform reports are
identified by `state/current.json` and the latest iteration entry. Earlier
arithmetic and proof gates remain in place; a network replay pass never replaces
them.
