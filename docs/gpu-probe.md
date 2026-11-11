# First INT8 GPU instruction probe

This document records the small instruction probe from iteration 1. The full
K=4096 component added in iteration 2 is documented in `docs/fullk-kernel.md`.

**Iteration 1 status: CUDA source compiled for SM120 / GPU execution UNRUN.**
An isolated NVIDIA CUDA 13.5.37 toolchain compiled the source successfully.
The workspace has no accessible NVIDIA GPU. The host layout test was also
compiled and executed; neither result validates physical GPU execution.
See `reports/iteration-01-validation.json` and the compile receipt for evidence.

`native/cuda/mma_probe.cu` is a bounded correctness probe for the instruction:

```text
mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32
```

It is not a Pearl miner, NoisyGEMM implementation, proof generator, pool test,
throughput benchmark, or evidence that an optimized kernel works. In particular,
its K=32 and K=64 snapshots are arithmetic checkpoints, not Pearl transcripts.

## Exact layout source

The mapping follows NVIDIA's [PTX ISA, section 9.7.16.5.10: Matrix Fragments for
mma.m16n8k32](https://docs.nvidia.com/cuda/parallel-thread-execution/#warp-level-matrix-fragment-mma-16832),
signed eight-bit operands and INT32 accumulators (PTX ISA 9.5, checked 2026-11-11).
Figures 91, 93/94 and 95 describe A, B and accumulators respectively. NVIDIA's
[CUTLASS atom](https://github.com/NVIDIA/cutlass/blob/main/include/cute/arch/mma_sm80.hpp)
`SM80_16x8x32_S32S8S8S32_TN` supplies a second source for the instruction signature
and register counts. There is no CUTLASS build dependency here.

Let `g = lane / 4` and `t = lane % 4`. Each A/B register packs four signed bytes,
with the first element in the least significant byte. For one K32 segment:

| Register | Rows | Columns |
|---|---|---|
| A0 | g | 4t through 4t+3 |
| A1 | g+8 | 4t through 4t+3 |
| A2 | g | 4t+16 through 4t+19 |
| A3 | g+8 | 4t+16 through 4t+19 |
| B0 | 4t through 4t+3 | g |
| B1 | 4t+16 through 4t+19 | g |
| D0, D1 | g | 2t, 2t+1 |
| D2, D3 | g+8 | 2t, 2t+1 |

The second operation adds 32 to A's K coordinate and B's K coordinate. Host
matrices are row-major A[16][64] and B[64][8]. The explicit packing implements
the instruction's `row.col` register layout; it does not reinterpret contiguous
row-major B bytes as a column-major matrix.

## What executes

For each of four deterministic cases (zeros, asymmetric mixed signed values,
INT7 endpoints -64/+63, full INT8 endpoints -128/+127), the program:

1. Copies the small matrices to device buffers and poisons the output buffer.
2. Launches exactly one block of 32 threads, one fully participating warp.
3. Runs two consecutive MMA operations with INT32 accumulation, retaining the
   first result rather than resetting the accumulator before the second.
4. Copies all 128 results at K32 and all 128 at K64 back to the host.
5. Compares every value exactly against a separate naïve row-major CPU loop
   using INT64 intermediates. The input bound makes INT32 overflow impossible.

Four launches and 1,024 checked output values complete the probe. There are no
persistent kernels, polling loops, frequency changes, or TDR configuration
changes. CUDA setup, allocation, transfers, launch, synchronization, explicit
cleanup, and comparison errors produce a nonzero exit code. The probe defaults
to device 0; `--device N` selects exactly one device. The runtime verifies compute
capability >=8.0 and warp size 32. The binary must also be compiled for the actual
target; checking the device property does not make an incompatible cubin run.

The intended GeForce target is `sm_120`, with CUDA C++17. The repository's CUDA
build option is off by default. Building this target requires a compatible CUDA
toolkit on the target system. Linux `nvcc` compilation was checked in iteration1;
Windows compilation and physical execution remain separate pending checks.

The process prints one JSON result with schema `neuro.gpu_instruction_probe.v1`.
A successful physical run sets `exact: true` and `gpu_execution_observed: true`;
it always sets `mining_evidence: false` and `hashrate: null`. Failures report an
error and `exact: false`. Capture the actual process result with an external
timeout. A finite kernel is not a guarantee that a driver/OS fault cannot hang
the host; the process itself cannot recover an unresponsive operating system.

## Host test and its limit

`native/tests/mma_layout_test.cpp` checks fragment coverage and sign decoding,
packs all warp registers, emulates the instruction through independently written
inverse register lookups, scatters output and compares both checkpoints with the
naïve reference. It exercises all four fixture cases, including the second K
segment. Its JSON says `cpu_layout_emulation` and
`gpu_execution_observed: false` explicitly.

This test can catch packing/scatter mistakes without a GPU. It does not compile
inline PTX or prove actual hardware instruction semantics. The next physical
gate is compiling the CUDA target on Windows and running the GPU probe on the laptop.

## Reproducing the compile-only check

The isolated Linux check used NVIDIA's published Python wheels (installed outside
the repository): `nvidia-cuda-nvcc`, `nvidia-cuda-runtime`, `nvidia-cuda-crt`, and
`nvidia-nvvm` at `13.5.37`; `nvidia-cuda-cccl` at `13.3.5.0.6`. These are a checked
compiler configuration, not a requirement to replace the laptop's driver.

With that toolchain's `nvcc` on PATH:

```bash
nvcc -std=c++17 -arch=sm_120 -O2 -lineinfo -Xptxas=-v \
  -I native/include -c native/cuda/mma_probe.cu -o build/mma_probe_sm120.o
```

No GPU is needed to compile this object. No hashrate or execution result follows
from a successful compilation.
