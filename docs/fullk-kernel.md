# Full-K INT8 arithmetic component

Iteration 2 implements a finite CUDA baseline for K=4096 and its cumulative
milestone transcript. It compiles and links for SM120 on Linux. Independent CPU
checks pass. **Physical GPU execution, Pearl input generation, proof and pool
acceptance are not established by this component.**

## Input and output contract

The shared host contract is `native/include/neuro/fullk.hpp`; the CUDA entry
point is declared in `native/include/neuro/fullk_cuda.cuh`.

| Quantity | Layout / constraint |
|---|---|
| A | Contiguous signed INT8, row-major `[M][4096]` |
| Bt | Contiguous signed INT8, row-major `[N][4096]`; transposed B storage |
| M, N | Multiples of 16, inclusive range 16 through 1024 |
| K | Exactly 4096 |
| C | Cumulative signed INT32 dot products; optional final output `[M][N]` |
| Milestone | After each 128 K elements, retaining all previous accumulation |
| Transcript word | XOR of unsigned 32-bit representations of an 8x16 region of C |
| Transcript layout | Milestone, spatial tile row, spatial tile column |

For zero-based milestone `s`, the C value includes K positions 0 through
`(s+1)*128-1`. There are 32 milestones. For tile row `tr` and tile column `tc`:

```text
tiles = (M / 8) * (N / 16)
transcript[s * tiles + tr * (N / 16) + tc]
```

The word is the XOR of all 128 C values in that tile at that milestone. Values
are converted to UINT32 before XOR. This includes negative INT32 values without
depending on signed bitwise behavior. K=4096 signed INT8 dot products are bounded
in absolute magnitude by 4096*128*128 = 67,108,864, so INT32 overflow is impossible
under this contract. The independent scalar oracle still uses INT64 accumulation.

The launch function accepts caller-owned **device buffers** and an optional CUDA
stream. It allocates no memory, performs no copies and does not synchronize.
`a`, `bt` and `transcript` must be non-null; `final_c` may be null. Buffer length,
device ownership, non-overlap and lifetime are the caller's responsibility.
Invalid shapes or required null pointers return `cudaErrorInvalidValue` before
any CUDA runtime call in the function. Valid calls return immediate launch status;
the caller must also check stream/device synchronization before trusting output.

The 1024 bound is a prototype limit, not a mining-panel size. A later scheduling
adapter must construct or stage appropriate panels. A working small arithmetic
launch does not implement that scheduler.

## Kernel organization

`native/cuda/fullk_kernel.cu` assigns one complete warp to a 16x16 output region.
Grid X covers columns and grid Y covers rows. Each K32 step packs A once and
executes two `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32` instructions, one
for each eight-column half. Both accumulator fragments persist for all 128 K32
steps. Four K32 steps complete one milestone.

At each milestone, two full-mask warp XOR reductions produce the upper and lower
8x16 tile words. Lane 0 writes them at their global transcript indices. If
`final_c` is present, every lane scatters its final accumulators after the last
milestone. The layout helpers and their PTX source are documented in
`docs/gpu-probe.md`.

The direct kernel uses explicit byte packing and direct global loads. A shape
whose rows and columns are both multiples of 64, and whose A and Bt pointers
are 16-byte aligned, uses a different launch: 512 threads stage one milestone
(128 K values) of a 64x64 output tile through shared memory, then each warp
runs the same 16x16 MMA, transcript XOR, and final-C scatter. Fragment
words in that staged tile are 4-byte aligned, so each one is a single
little-endian shared load rather than four byte loads. Stored rows use a
144-byte stride so successive rows do not land on the same shared bank.
Unaligned
pointers and every other legal shape, including the 32x48 gold fixture, stay
on the direct kernel. Asynchronous copies and a persistent kernel remain later
candidates. Compiler output reports 80 registers and no spills for the recorded
direct SM120 object; these counts are not a speed estimate. The staged kernel
is a separate launch and its register count is not that figure.

## Independent correctness checks

Four distinct calculations constrain the implementation:

1. `tools/generate_fullk_fixtures.py` computes dense incremental scalar results
   using Python integers. It writes fixed expected arrays only with `--write`.
2. `native/reference/fullk_reference.cpp` computes cell-wise dot products in
   INT64, independent of GPU packing, register coordinates and reduction code.
3. `native/reference/fullk_warp.cpp` emulates packed registers, MMA inverse
   lookups, warp XOR and final scatter. It exercises the CUDA data mapping on CPU.
4. `native/tests/fullk_upstream_test.cpp` compares larger cases against the
   unchanged CPPminer scalar/exact-signed/separate-prepack transcript path.

The six committed fixture cases are zeros, mixed signed values, INT7 edges, INT8
edges, values at milestone boundaries, and asymmetric coordinate-derived values.
Their dimensions range from 16x16 through 32x48. They contain 1,024 expected
transcript words and 4,096 final matrix values. Comparing both C++ paths to these
arrays yields 10,240 exact comparisons; 51 malformed-input cases are rejected.
Checking final C is necessary because XOR can hide element permutations, and
some symmetric edge fixtures legitimately have zero transcript words.

The donor differential uses four larger cases at 128x128 or 128x256. It compares
24,576 transcript words from each of the two C++ paths against CPPminer and
98,304 final cells between our independent CPU paths: 147,456 comparisons total.
CPPminer exposes transcript in this interface, so its final C is not separately
compared. The donor reference checkout remains pinned and unchanged.

The fixture manifest includes the SHA-256 of A/Bt bytes, little-endian transcript
and final C, the generator, and the golden header. These are **synthetic arithmetic
vectors**, not protocol-generated matrices, commitments or valid mining work.

## Physical GPU verification entry point

Build as described in `docs/development.md`. On the actual CUDA host, run the
small instruction probe before the full-K probe, capturing output and exit status
with an external process timeout. Example on Linux:

```bash
timeout 30s ./build/cuda/neuro_mma_probe --device 0
timeout 60s ./build/cuda/neuro_fullk_probe --device 0
```

Windows executables have `.exe` and may be under `Release/`; a Windows/SSH worker
must impose its own process timeout. That worker is scheduled for a later
iteration. Neither finite kernels nor process timeouts can recover a hung OS.
Use the laptop's existing driver/power profile and small fixtures first.

The full-K probe can select one fixture with `--case coordinate_mix`. With no
case restriction, it tests all six cases twice: once writing final C and once
with `final_c=nullptr`. It poisons output buffers before each launch and compares
every output with the committed Python-generated expectations. A complete
physical PASS would report 12 launches, 2,048 transcript word comparisons and
4,096 final C comparisons. That is an expected future result, **not an observed
GPU result from iteration 2**.

Success emits schema `neuro.fullk_probe.v1` with `exact=true`, the actual device,
compute capability and completed checks. Failures return nonzero and emit JSON.
The program always reports `mining_evidence=false` and `hashrate=null`. It
generates no noise or proofs and does not connect to a pool.

The separate `--host-validation` mode rejects 12 invalid calls without launching
GPU work. This mode passed in the no-GPU build environment and is the only CUDA
probe mode registered in CTest. It cannot substitute for a physical run.

## Evidence and next boundary

See `reports/iteration-02-validation.json` and
`reports/fullk-sm120-compile.json` for source hashes, commands and coverage.
AddressSanitizer and UndefinedBehaviorSanitizer passed for the small CPU fixture
test; LeakSanitizer could not run in this ptrace environment and was disabled.

Iteration 3 adds the deterministic Pearl signal/noise/commitment path as a
separate caller of this unchanged arithmetic contract. See `docs/pearl-input.md`.
Real proof verification remains a distinct gate; fixture success is not proof
or share acceptance.

`--time-launches N` (8 through 64) requires one `--case`. It checks that gold
fixture once, then records CUDA events around N queued launches of the same
kernel. Host copies, poison memsets, and the compare stay outside the interval.
The JSON schema is `neuro.fullk_kernel_time.v1`. It always sets
`mining_evidence` false and `hashrate` null. `tools/record_gpu_baseline.py`
turns three such samples for `coordinate_mix` into one `neuro.experiment.v1`
baseline. That record is a kernel interval, not accepted pool work.
The recorder publishes only after each probe process completed successfully:
zero integer return code, no timeout or launch error, a fully drained and
untruncated stdout, and a valid observation ID. Verification and every sample
must be distinct processes; a `PASS` line alone is not enough, and a failed run
leaves an existing artifact byte-identical.

`--panel-rows R --panel-cols C` times a different workload. R and C must be
multiples of 128 in [128, 1024], the range shared by this kernel and the Pearl
input contract. The panel is `coordinate_mix` with seed `0x12345678`. Expected
transcript and final C come from `native/reference/fullk_reference.cpp`, not
from the small gold header. Omit `--time-launches` to verify once with final C
and once without it. With `--time-launches`, the scalar reference and the
compare stay outside the same CUDA-event interval. `record_gpu_baseline.py
--panel` records three samples of the 1024x1024 panel to
`artifacts/gpu-panel-baseline.json`. The 32x48 gold baseline stays a separate
experiment. Neither record is a hashrate or a pool share.
