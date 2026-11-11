# Architecture and current implementation

Current preparation: iteration 2 of 10. Read `state/current.json` for the next
task and `docs/iterations/02.md` for observed evidence. Build commands live in
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

The immediate native checks build selected upstream components directly. They
do not invoke upstream installers, connect to a pool, or link proof stubs.
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
| Orchestration | Existing DSH + small Python stdlib tools | Run bounded experiments, capture observations, keep project state |
| Miner host | Reuse CPPminer C++ | Job lifecycle, pool connection, GPU/proof handoff |
| GPU math | CUDA C++17 + selective inline PTX | Exact INT8/INT32 matrix work and intermediate transcript |
| Proof | Existing Rust proof code | Generate and independently verify the protocol's required evidence |
| Model cost | Saved final usage + exact Decimal pricing | Track observed usage without inventing cache savings |

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

The next integration boundary is deterministic Pearl matrices, noise and
commitments. Synthetic arithmetic fixtures do not exercise that boundary.
Large-panel scheduling, production memory staging and performance tuning follow
correct integration; accepting larger dimensions alone would not supply them.

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

Start with `openai/gpt-6.1-sol`, conservatively priced at the owner's ordinary
input/output rates. Its route, context limit and cache billing are unverified.
The 64k input budget is our task-size policy, not a claim about its maximum
context. Trial MiniMax M3 on the same bounded coding/repair tasks before letting
it take over. Score verified useful work and total retry cost, not token price
alone. A fallback model must not silently multiply the spending budget.

Initial proposed limits are $5 per work session and $25 for the first campaign,
with bounded calls and context. These are configuration intentions. The DSH
bridge must reserve a worst-case request cost and reconcile real usage before
we can call them enforced limits. This bridge is not implemented yet.

## Open inputs for the handoff

- Actual DSH version/launch interface and usage events.
- API endpoint and chosen route's real response/usage/cache semantics.
- SSH host alias, Windows project path, CUDA toolchain and driver inventory.
- Pool and wallet configuration, kept outside the public repository.
- Fresh network protocol/test vectors before final integration; protocol drafts
  are not activation evidence.

These missing private/live inputs do not block local source, correctness, CUDA
compile, or packaging work. Record each precisely; do not create fake values.
