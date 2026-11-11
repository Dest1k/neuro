# Upstream audit: iteration 1

Audit date: **2026-11-11 UTC**. Target repository: public `alinescafs3mp-afk/neuro`.

## Decision

Use **CPPminer as a pinned, read-only Git submodule and MIT-licensed host/protocol donor** at `upstream/cppminer`. Compile a small CPU-only subset first; retain the upstream notices and record the exact commit and important blob IDs. Keep neuro's adapters and future CUDA backend outside the upstream checkout.

For the intended NVIDIA SM120 backend, plan an independently implemented CUDA C++ kernel. CPPminer's current CUDA code is a Pascal-oriented SIMT/DP4A implementation and is not evidence of an efficient RTX 5080/5090 Tensor Core miner. Open-Pearl-Miner contains a Tensor Core implementation, but its custom fee-retention license makes it unsuitable for the chosen permissive public baseline without a separate licensing decision.

The parent task materialized the pinned upstream checkout. This audit compiled and ran the upstream CLI and protocol tests and an independently written scalar arithmetic comparison probe using GCC 14.2.0. It ran no miner, used no GPU, and contacted no pool. Full-miner, proof, and pool acceptance remain unverified.

## Immutable sources

| Source | Inspected main commit | Commit date (UTC) | Git tree |
| --- | --- | --- | --- |
| CPPminer | [`5b68506af5e6433ee5c25aa9e6fbb00b537590aa`](https://github.com/1640675651/CPPminer/commit/5b68506af5e6433ee5c25aa9e6fbb00b537590aa) | 2026-11-07 04:55:41 | `751efc5e67570b7f088c581946c22e4a2628aa00` |
| Open-Pearl-Miner | [`ac3d992d7eb6ca1bb344d5d3ee125eecb182248d`](https://github.com/Muskwak/Open-Pearl-Miner/commit/ac3d992d7eb6ca1bb344d5d3ee125eecb182248d) | 2026-06-19 06:03:29 | `017c15d1e5d123bb0110ce949302978986245f33` |

Both recursive trees were retrieved with `truncated: false`. Commit metadata, trees, and inspected file contents were read using the GitHub connector's structured response. Dates above describe upstream commits; they do not imply that runtime behavior was verified on those dates.

[The source lock](../upstream/sources.lock.json) records exact Git object IDs, byte sizes, checkout paths, compiled subsets, exclusions, and unresolved dependencies. Git blob IDs are hashes of Git objects, not plain-file SHA-1 digests.

## Licensing and import boundary

### CPPminer

The inspected [root license](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/LICENSE) is MIT, copyright 2026 foolzhz. Preserve that license with copies or substantial portions of covered code. The upstream miner implements a developer fee, but fee retention is not a condition in this MIT license.

Do not relabel all vendored dependencies as MIT. The proof package depends on `third_party/pearl-blake3`, `third_party/zk-pow`, and `third_party/plonky2`. The inspected tree contains MIT/Apache license files for plonky2 but no dedicated license file inside the two vendored Pearl directories. Their original files remain in the upstream checkout. Reconcile their original upstream provenance and license text before extracting or redistributing that proof dependency tree as part of neuro's own build.

### Open-Pearl-Miner

The inspected [Pascal Pearl Miner License](https://github.com/Muskwak/Open-Pearl-Miner/blob/ac3d992d7eb6ca1bb344d5d3ee125eecb182248d/LICENSE) requires retaining the original **2% developer fee** in distributed derivatives. Its personal-use and self-mining exceptions expressly exclude publication and distribution. A public neuro repository is outside that private-use scenario.

**Decision for this baseline: import no Open-Pearl-Miner code.** Its license and source shape are audit evidence, not permission to copy the GPU kernel into a fee-free public derivative. This also avoids silently carrying a custom licensing condition into MIT-origin host code.

## What CPPminer provides

| Component | Evidence at the pinned commit | Integration judgment |
| --- | --- | --- |
| CLI parsing regression tests | [`include/cp_cli.h`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/include/cp_cli.h), [`tests/cp_cli_test.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/tests/cp_cli_test.cpp) | Immediate small build subset; standard C++17 only. |
| Pool framing and notify parsing tests | [`tests/cp_pool_protocol_test.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/tests/cp_pool_protocol_test.cpp), [CMake test targets](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/CMakeLists.txt) | Immediate offline regression baseline, separate from network mining. |
| TCP transport tests | [`tests/cp_tcp_test.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/tests/cp_tcp_test.cpp) | Later loopback transport stage; not needed for the first offline subset. |
| Pool session, share submission, and work lifecycle | [`src/common/cp_pool.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/common/cp_pool.cpp), [`include/cp_pool.h`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/include/cp_pool.h) | Useful host donor; remove project coupling deliberately when adapting. |
| CPU arithmetic/reference path | [`case33_gemm_xor.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/cpu/gemm/case33_gemm_xor.cpp), [header](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/cpu/gemm/case33_gemm_xor.hpp) | Scalar and SIMD comparison basis; not a standalone single-file drop-in. |
| Plain-proof construction and verification | [`rust/cp-proof-ffi/Cargo.toml`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/rust/cp-proof-ffi/Cargo.toml), [`include/cp_proof.h`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/include/cp_proof.h) | Candidate Rust static library behind a C ABI, after dependency and license reconciliation. |
| CUDA GEMM | [`cp_cutlass_gemm.cu`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/cuda/cp_cutlass_gemm.cu), [CUTLASS types](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/cuda/cutlass/cp_cutlass_gemm_types.h) | Exclude from the initial build; not the target fast SM120 kernel. |

The upstream host currently includes both Pearl and Quantus features. The submodule makes that full source tree available for inspection, but the first build links only selected files. Later adapters must account for unrelated algorithms and backend/developer-fee coupling instead of assuming the full miner is a Pearl-only library.

The pool implementation includes Kryptex v2 negotiation and gzip-wrapped proof submission. This is useful protocol evidence, but it does not prove current acceptance by any pool. Pool behavior and proof format must later be checked against a saved job and the intended endpoint without conflating local hashes, local proof validity, and accepted shares.

## First build: exact CPU-only subset

Checkout: `upstream/cppminer/`, with original relative paths and unmodified upstream files. The following 16 files define the license and dependency closure for the two initial regression binaries. Each entry is pinned by blob ID in the lock. The submodule contains the complete upstream tree; this list is the compiled subset, not an assertion of a partial checkout.

- `LICENSE`
- `include/cp_cli.h`
- `tests/cp_cli_test.cpp`
- `tests/cp_pool_protocol_test.cpp`
- `include/cp_json_frame.h`
- `include/cp_json_text.hpp`
- `include/cp_pool.h`
- `include/cp_state.h`
- `include/cp_submit_table.hpp`
- `include/cp_util.h`
- `include/cp_config.h`
- `include/cp_platform.h`
- `src/common/cp_json_frame.cpp`
- `src/common/cp_pool_notify.cpp`
- `src/common/cp_util.cpp`
- `src/common/cp_state.cpp`

The include closure was checked for this subset. The compile source lists below match the upstream CMake test targets. These tests do not require BLAKE3, CUDA, Cargo, or CMake.

### Minimal Linux build commands

From the neuro root, with the pinned submodule checkout present:

```bash
mkdir -p build

g++ -std=c++17 -O2 -Wall -Wextra -I upstream/cppminer/include \
  upstream/cppminer/tests/cp_cli_test.cpp \
  -o build/cp_cli_test

g++ -std=c++17 -O2 -Wall -Wextra -pthread \
  -I upstream/cppminer/include \
  upstream/cppminer/tests/cp_pool_protocol_test.cpp \
  upstream/cppminer/src/common/cp_json_frame.cpp \
  upstream/cppminer/src/common/cp_pool_notify.cpp \
  upstream/cppminer/src/common/cp_util.cpp \
  upstream/cppminer/src/common/cp_state.cpp \
  -o build/cp_pool_protocol_test

./build/cp_cli_test
./build/cp_pool_protocol_test
```

Do not define `NDEBUG`: the upstream tests use assertions for their checks. These commands target the current Linux workspace; native Windows linking is different. The upstream CMake targets add Windows system libraries such as `bcrypt` and `ws2_32` where needed.

Both commands compiled successfully and both binaries exited 0 in this workspace. The protocol compile emitted upstream `cp_net_init` unused-function warnings; there were no compile errors. A passing CLI/protocol test confirms only that subset. It says nothing about matrix arithmetic, GPU execution, proof correctness, pool acceptance, or performance.

## CPU reference and mock limitations

The CPU core exposes `Case33Isa::Scalar` alongside hardware-specific implementations. The algorithm processes 8-by-16 semantic tiles and rank milestones along K. Defaults in [`cp_config.h`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/include/cp_config.h) include `K_DIM=4096` and `R_RANK=128`; these are implementation defaults, not proof that all future jobs use those values.

The [diagnostic dispatch](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/common/cp_diagnostics.cpp) provides `--simd-test`, `--prepack-test`, and mock operation. The SIMD parity test compares implementations with the scalar result. The CPU source also depends on noise/job-control support and multiple ISA implementations, so importing only the two `case33_gemm_xor` files is insufficient for a linked reference executable.

The upstream prepack test uses large matrices and is not an appropriate first smoke test. neuro now has a bounded independent oracle in `native/tests/cppminer_math_probe.cpp`, compared directly with the upstream scalar path as described below. This is arithmetic component verification, not full Pearl proof verification.

The full [CMake build](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/CMakeLists.txt) defaults to CPU enabled and requires fetched BLAKE3 C sources. The [build helper](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/build.sh) selects BLAKE3 1.8.5 and, for the optional CUDA backend, CUTLASS v2.11.0. These dependency tags are upstream declarations; this audit did not resolve or download them independently.

Crucially, CMake falls back to [`cp_proof_stub.cpp`](https://github.com/1640675651/CPPminer/blob/5b68506af5e6433ee5c25aa9e6fbb00b537590aa/src/common/cp_proof_stub.cpp) when Cargo/proof support is unavailable. The stub returns failure for proof construction and verification. A successful full-miner build with this stub must not be presented as a valid-proof or accepted-share milestone. The current workspace lacks CMake, Cargo, and NVCC, so a real full build and GPU check require an explicit later environment step.

## Verified scalar arithmetic probe

[The native probe](../native/tests/cppminer_math_probe.cpp) forces `Case33Isa::Scalar`, exact signed-int8 arithmetic, and separate prepacking. The smallest accepted upstream macro dimensions are **128 by 128**, with K=4096. The independent oracle reads raw A and **transposed B (N by K)** directly, accumulates each output cell in int64, range-checks the result, and XOR-reduces each 8-by-16 tile at every cumulative rank-128 milestone. It reuses no upstream packing or arithmetic implementation.

Five deterministic cases passed:

| Case | M by N | Compared milestone/tile words | Purpose |
| --- | --- | --- | --- |
| Zero | 128 by 128 | 4,096 | Empty arithmetic baseline. |
| Signed int7 boundaries | 128 by 128 | 4,096 | Mixed signs and values including -64 and 63. |
| Signed int8 extremes | 128 by 128 | 4,096 | Arithmetic stress including -128 and 127; not a claim about valid Pearl-generated matrix values. |
| Cumulative rank boundaries | 128 by 128 | 4,096 | Nonzero products at the first and last K of each milestone, with changing signs. |
| Rectangular two macros | 128 by 256 | 8,192 | Asymmetric input, B transpose, spatial tile order, and multiple macros. |

Total: **24,576 exact uint32 comparisons**. The probe exited 0. This is a small correctness check, not a timing benchmark.

The verified Linux/GCC 14.2.0 command is:

```bash
g++ -std=c++17 -O2 -flto -ffunction-sections -fdata-sections \
  -I upstream/cppminer/include \
  -I upstream/cppminer/src/cpu/gemm \
  native/tests/cppminer_math_probe.cpp \
  upstream/cppminer/src/cpu/gemm/case33_gemm_xor.cpp \
  upstream/cppminer/src/cpu/gemm/case33_cpu_features.cpp \
  -Wl,--gc-sections -o build/cppminer_math_probe

./build/cppminer_math_probe
```

LTO is significant here: with scalar/separate mode fixed by the probe, it removes unused SIMD and fused-noise branches. No replacement noise/hash/proof stubs were used, and no extra dependency was downloaded. Ordinary separate compilation without whole-program optimization can retain those references and require the wider upstream dependency closure. This command is verified for the stated Linux/GCC environment, not a Windows build recipe or a portable promise for every linker.

## GPU assessment and SM120 work

CPPminer's CUTLASS type definitions select `cutlass::arch::Sm61`, `OpClassSimt`, and an instruction shape of `1 x 1 x 4`. Its capability guard permits pre-SM70 devices and SM70/SM75; it does not select this path for SM120. This is concrete evidence that reusing the host does not deliver an RTX 5080/5090 Tensor Core backend.

Open-Pearl-Miner contains [`csrc/gemm/pearl_ampere_tc.cu`](https://github.com/Muskwak/Open-Pearl-Miner/blob/ac3d992d7eb6ca1bb344d5d3ee125eecb182248d/csrc/gemm/pearl_ampere_tc.cu), a fused INT8 Tensor Core implementation. However, its [C ABI build helper](https://github.com/Muskwak/Open-Pearl-Miner/blob/ac3d992d7eb6ca1bb344d5d3ee125eecb182248d/packaging/build_capi.sh) defaults to `sm_61, sm_80, sm_86, sm_89`, not `sm_120`. Its published implementation is evidence of a possible architecture, not a verified SM120 port or a performance result on the target laptop.

For neuro, implement the new CUDA C++ backend behind a small job/result interface and compare its exact arithmetic against a deterministic CPU reference before attempting live submission. CUDA compiler/toolkit selection, CUTLASS version, kernel tiling, and Windows integration remain later work. No GPU speed claim follows from this audit.

## Deferred imports and acceptance boundaries

1. **Protocol subset — complete:** use the pinned upstream submodule, retain MIT notices, compile and run both offline tests.
2. **CPU arithmetic — bounded check complete:** five deterministic fixtures match the pinned scalar implementation. Noise, matrix generation, hash commitments, and cryptographic proofs are outside this probe.
3. **Proof bridge:** import the C header and Rust FFI package only after reconciling vendored Pearl/plonky2 provenance and licenses. Preserve Cargo.lock and all dependency notices. A proof stub is a failure path, not a verifier.
4. **GPU backend:** use independently implemented CUDA C++ with explicit SM120 targeting; require CPU/GPU equality on fixed vectors.
5. **Pool integration:** validate a complete job-to-proof-to-accepted-share path separately from synthetic throughput tests.

There is no neuro mining-throughput measurement, no accepted share, and no demonstrated SM120 correctness at this audit stage.
