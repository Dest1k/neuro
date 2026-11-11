# Architecture and current implementation

Current preparation: iteration 7 of 10. Read `state/current.json` for the next
task and the latest entry in `docs/iterations/` for observed evidence. Build commands live in
`docs/development.md`; the root README is intentionally empty at the owner's request.

## Goal

Produce a personally operated Pearl miner through an autonomous model-driven
development loop. The first hardware profile is an RTX 5080 Laptop, compute
capability 12.0, on Windows. A Linux VM runs the existing DSH and controls the
test host over SSH. Human CUDA development is outside the available resources.

The preparation comprises ten web-chat iterations. The tenth deliverable is a
complete DSH handoff archive and starting directive. This is not a promise of
beating a commercial miner without GPU measurements.

## Reuse boundary

CPPminer is pinned as `upstream/cppminer`. Its own host/protocol code is MIT.
Its source tree includes the CPU implementation, Rust proof interface, and other
dependencies. Keep their origin and license notices. Our root MIT license does
not relicense third-party content.

The native checks build selected upstream components. CMake prepares a
hash-checked copy of `cp_pool.cpp` with the unused legacy asynchronous reader
excluded; `tools/prepare_bounded_pool.py` applies the recorded patch only under
`build/`. Connect/receive/serializer functions retain their donor implementation.
The
official BLAKE3 C implementation is separately pinned as `upstream/blake3` and
compiled with its portable backend. The checks do not invoke upstream installers,
connect to a pool, or link proof stubs.
Changes to our implementation belong in this repository. Future modifications
to the donor should be stored as reviewed patch files and applied to a separate
working checkout; the reference checkout stays pinned and clean.

The current upstream CUDA kernel uses a Pascal/SIMT path and a hardware guard
that excludes SM120 for that path. Preserve it as source evidence; build a
separate Tensor Core implementation instead of treating `-arch=sm_120` as an
optimization. Open-Pearl-Miner was reviewed but not imported because its custom
distributed-derivative license requires the original 2% fee.

## Layers

| Layer | Choice | First responsibility |
|---|---|---|
| Orchestration | Existing DSH + small Python tools and fixed Windows actions | Run bounded experiments, capture observations, keep project state |
| Miner host | Pinned CPPminer wire code + small C++ coordinator | Immutable work identity, cancellation, GPU/proof handoff and exact response accounting |
| GPU math | CUDA C++17 + selective inline PTX | Exact INT8/INT32 matrix work and intermediate transcript |
| Proof | Existing Rust proof code | Generate and independently verify the protocol's required evidence |
| Model cost | Durable SQLite reservations + exact Decimal pricing | Reserve before dispatch, reconcile final usage and retain uncertain charges |

Do not rewrite functioning host or proof code just to use a new language.
Do not implement a replacement LLM harness or a new network protocol.

## Arithmetic correctness boundary

The CPU component check compares cumulative INT8 products and tile XORs against
an independent, simple int64 oracle. It uses the pinned donor's K=4096, milestone
step128, and 8x16 spatial tile contract. This is a component-level assertion:
matrix generation, noise generation, commitments, jackpot, proof and share
acceptance are additional checks, not implied by it.

The first GPU probe exercises one warp and two `mma.sync.m16n8k32` operations.
The second implements the full K=4096 arithmetic component with two N8 MMA
operations per K32 step, retaining cumulative INT32 accumulators and all 32
milestone XORs. Its inputs are contiguous A[M][4096] and transposed Bt[N][4096].
One warp owns a 16x16 region; M and N must be multiples of 16 in [16,1024].
This bounded baseline has direct global loads and no performance claim.

The full-K implementation has an independent Python scalar fixture generator,
a separate C++ int64 oracle, and a CPU emulator of packing/MMA/reduction/scatter.
The committed fixtures check both milestone words and all final matrix cells;
XOR alone could hide element permutations. Larger CPU cases also agree with the
pinned CPPminer scalar transcript. CUDA compilation and full Linux linking for
SM120 pass; physical GPU execution is still unobserved. See `docs/fullk-kernel.md`.

Iteration 3 connects the real donor signal/commitment/noise functions to the
full-K interface. A bounded first zero-B attempt resets signal matrices, mutates
one A row, commits the signals, derives legacy or V3 seeds and constructs noisy
A/Bt. Three fixed synthetic job fixtures are checked against an
independent Python implementation, then against full donor builders and scalar
arithmetic. The CUDA consumer is compiled and its host preparation mode passes;
its physical GPU mode remains unrun. See `docs/pearl-input.md`.

Iteration 4 builds the genuine Rust PlainProof and witness implementations.
The native adapter selects tiles using independent jackpot vectors, generates
Merkle proofs from signal data, and verifies them both with the donor FFI and
with a separately pinned official Pearl verifier. All three synthetic winning
proofs pass both paths; losing and deliberately corrupted cases are rejected.
This verifies the bounded dense proof path, without generating a recursive
STARK or establishing live pool acceptance. See `docs/proof.md`.

Iteration 5 connects that path to actual TCP exchanges with a local synthetic
pool. Work snapshots include the entire header, target, certificate, shape,
encoding and connection; monotonic generations prevent stale local results from
being submitted. A selected winning tile is independently recomputed before
the real proof/verification path. Exact pending request IDs distinguish accepted,
rejected and unacknowledged submissions. The peer verifies received proofs with
the separate official checker against its own issued-job ledger. See
`docs/host-replay.md` for the bounded donor dialect and recovery scenarios.

The same host accepts our CUDA compute callback. The combined SM120 executable
compiles and links on Linux; its CPU backend passes the real loopback exchange.
This provides a hardware-test entry point without claiming observed GPU work.
Iteration 6 adds a native Windows MSVC proof profile, a fixed Ninja/Release
build/run entry point and a bounded Linux-to-Windows SSH worker. The Windows
profile matches the Rust release CRT and checks compiler, lockfiles, library
hash and configuration before linking. The complete Windows CPU/proof/loopback
path passed on an actual GitHub Windows runner. The tested commit and evidence
are recorded separately in `reports/iteration-06-validation.json`.
`docs/windows-worker.md` describes the implemented recipe and evidence contract.

The worker dispatches only inventory, build-probes, run-cpu and run-cuda actions.
A Windows Job Object owns each action's descendants, output and lifetime. Saved
source/binary fingerprints bind later CPU/GPU gates to the successful build.
Iteration 7 extends the source fingerprint to the complete public candidate:
exact file bytes, dirty source additions/deletions and all pinned source files.
The SSH request and pre/post action checks bind this identity across both hosts.
Private settings, generated output and installed toolchains have their separate
boundaries. Source deployment must still preposition the complete checkout.
See `docs/candidate-identity.md` for the precise manifest and exclusions.
A lost SSH session is an uncertain outcome until a matching result is retrieved;
it never triggers an automatic duplicate GPU run. These components are
implemented, but the owner's laptop connection and physical GPU execution
still need their own observations.

The miner now supports a reproducible bounded sequence of up to 64 cumulative
zero-B attempts per generation. It retains prior A-row mutations, derives each
later row seed from a fixed root seed and counter, and seals the work and attempt
identity into each verified result. Misses advance the search; cancellation,
one handoff per attempt and independent proof checks remain in force. The old
attempt-zero fixtures remain unchanged. `docs/attempt-search.md` describes the
independent corpus and real TCP continuation cases. Sustained production search,
large-panel staging and performance tuning remain ahead.

Physical GPU actions additionally acquire a bounded ownership lease for the
owner's SRBMiner. A separate interactive watchdog prepares restoration before
stopping its exact configured processes; once test processes have exited, an
independent interactive task starts the existing `guided-setup.bat` in a visible
console. CPU/build actions do not acquire this lease. Actual owner-desktop and
miner restoration require observations on that host; CI fixtures are separate.
The private profile and recovery contract are in `docs/gpu-miner-handoff.md`.

## Measurement policy

Correctness precedes speed. Maintain separate baseline and candidate results,
repeat measurements under the same hardware/workload/power profile, and keep
the fastest verified candidate. Kernel timings and actual accepted pool work
remain separate metrics. Pool comparisons account for share difficulty and
elapsed observation time; short runs cannot prove a small speed advantage.

Use the laptop's existing power/clock profile. No automatic overclocking or
TDR disabling. A hung Windows host is beyond SSH's recovery capability; the
remote worker must state that failure and preserve the last completed record.

## Model policy

The owner selected `minimax/minimax-m3` for a second bounded trial. Its status is
`provisional_user_selected_retrial`; route identity, availability, cache usage
and billing are unverified. Both reservations and reconciled usage use ordinary
input pricing without a speculative cache discount. The 64k input and 8k output
limits are our request policy, not verified model context limits. Score accepted
changes and total retry cost. `openai/gpt-6.1-sol` is an explicitly enabled
fallback, disabled by default, with three calls and $0.50 per session inside
the shared budget.

`neuroctl budget` implements the local accounting boundary: reserve full maximum
request cost, grant one dispatch permission, then reconcile the final normalized
usage. SQLite transactions protect the shared $5/session and $25/campaign caps
and the 200-attempt session limit across processes and restarts. Missing usage
keeps its hold and stops further campaign dispatch; an observed overrun is
recorded in full and also stops new work. Prices and numerical limits are pinned
in the ledger. See `docs/model-selection.md` for the API, CLI and recovery rules.

The actual DSH/provider transport is not connected. Its adapter must enforce
the token bounds, route every paid retry through the boundary and supply complete
usage. Until that integration and billing are observed, the local gate is not
evidence that live DSH spending is controlled. No paid model calls were needed
to implement or test it.

## Open inputs for the handoff

- Actual DSH version/launch interface and usage events.
- API endpoint and chosen route's real response/usage/cache semantics.
- Resolve the owner's private SSH/access notes into the ignored worker profile;
  collect the actual Windows checkout, CUDA toolchain and driver inventory.
- Pool and wallet configuration, kept outside the public repository.
- Fresh network protocol/test vectors before final integration; protocol drafts
  are not activation evidence.

These missing private/live inputs do not block local source, correctness, CUDA
compile, or packaging work. Record each precisely; do not create fake values.
