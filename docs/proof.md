# Genuine Pearl PlainProof integration

Iteration 4 connects the retained Pearl signal/noise/arithmetic baseline to a
real Rust proof builder and a separately built official Pearl verifier. This
is an **offline dense PlainProof path**. It does not generate a recursive STARK,
execute a GPU, or demonstrate acceptance by a live pool.

The original Linux results and exact source hashes are recorded in
`reports/iteration-04-validation.json`. Iteration 5 connects this proof path to
the real local TCP replay; iteration 6 adds a native Windows MSVC build profile
and fixed build/run actions. The Windows workflow is being validated separately;
the earlier Linux report does not establish a Windows pass. Current gate results
belong in `state/current.json` and the latest iteration report. Source
relationships, differences and notices are in `docs/proof-provenance.md`.

## What the miner proves

`cp_proof_build` receives the original signal A and transposed signal Bt, not the
noisy matrices. It constructs a real Merkle multi-proof for the selected 8 A
rows and 16 Bt rows, then serializes the dense PlainProof with bincode and
base64. Building a proof does not establish that the tile won its target.

The verifier binds those strips to the submitted header/configuration and
commitment roots, derives the noise, recomputes the selected matrix tile and
jackpot message, and checks difficulty. `cp_proof_verify` is the actual pinned
donor Rust implementation. No `cp_proof_stub.cpp` enters this build.

The separate `pearl-proof-check` executable calls `verify_plain_proof` from
official Pearl commit `0dbdb2ffd8b81193a5079bb67a59aecc10ad501c`. Its Git dependency
is independent of the donor's vendored source path. It adds checks absent in
that donor snapshot, including tree-size binding and circuit/rank validation.
Both verifiers share Pearl concepts and the official BLAKE3 implementation;
this is an independent verification **path**, not an independent cryptographic
design or security audit. Passing this pinned reference does not establish
current network activation rules or pool-specific transport acceptance.

## Bounded native contract

The public adapter is `native/include/neuro/pearl_proof.hpp`:

| Operation | Purpose |
|---|---|
| `evaluate_tile` | Fold the arithmetic transcript into a 16-word jackpot, hash it, and compare with the scaled target |
| `build_plain` | Generate the real full-signal PlainProof for one tile |
| `make_witness` / `build_witness` | Construct bounded block/subroot witness input and feed the real Rust witness builder |
| `verify_plain` | Apply bounded format checks, then call the real Rust verifier |

Supported inputs are the first zero-B attempt from iteration 3, dense 8x16
tiles, K=4096, rank=128, certificate version 2 or 3, and M/N multiples of 128
between 128 and 1024. Observed end-to-end fixtures cover 128x128 and 128x256;
supporting larger bounds is not evidence of their tested performance.

The verifier's preflight limits base64 to 1 MiB and checks the exact dense wire
shape: matrix sizes, 32/64 Merkle chunks of 1024 bytes, row/leaf indices,
declared tree sizes, bounded sibling counts and the optional `None` suffix.
It refuses unsupported formats before the donor's unsafe FFI/deserializer.
Merkle authentication and the mathematical verdict remain Rust's work.
This adapter is deliberately narrower than a general network certificate
parser; MoE and other layouts are outside its contract.

The witness path computes genuine non-root BLAKE3 chaining values with the
pinned official C compression implementation. It preserves global chunk
counters and supplies the selected 256 KiB A block plus subroots and expected
roots. Zero Bt uses the Rust builder's documented zero-matrix cache. Three
witness proofs match their full-signal counterparts byte for byte. No GPU
witness generation or device-to-host transfer has been implemented here.

## Independent expected data and actual difficulty

`tools/generate_proof_fixtures.py` recomputes the iteration-3 scalar Python
signal/noise/transcript values, checks them against their retained manifest,
then derives jackpot messages and hashes with the pinned official Python
BLAKE3 binding. It never calls C++, the prover, or the verifier. Expected output
is committed in `native/fixtures/proof_golden.hpp` and `proof_manifest.json`;
read-only verification is the default.

For milestone i, the jackpot fold is:

```text
message[i % 16] = rotate_left_32(message[i % 16], 13) XOR transcript[i]
jackpot_hash = BLAKE3(16 message words in little endian, key = a_noise_seed)
```

The hash represents a little-endian U256. The pool target is an unscaled
big-endian U256. This rank-128 profile's factor is
`8 * 16 * (4096 / 128) * 128 = 524288 = 2^19`; overflowing scaled targets are
rejected by the adapter, never saturated to an automatically passing bound.

The synthetic test target is chosen **before** examining tiles: `2^235`, or
compact `nbits = 0x1e080000`. Its scaled bound is `2^254`. A real winner and
loser are selected from each of three jobs, after examining 512 distinct tiles.
All six selected tiles include the changed A row 26. Target selection does not
adapt to turn the expected loser into a winner. Separate boundary tests use
the exact smallest unscaled target accepting each independently known hash and
the value immediately below it.

The native check also compares the complete recomputed Rust jackpot hash and
all 16 message words, obtained on an intentionally rejected zero-target check,
with the independent golden data. This catches more than a permissive
accept/reject test could establish.

## Observed Linux checks from iteration 4

The Rust crate's 20 unit tests pass. The native proof suite generates six full
proofs and three matching witness proofs, checks 16,384 transcript words and
six independent jackpots, and exercises 69 rejected cases. Of those, 37 are
real semantic Rust rejections; the remainder exercise the bounded adapter or
FFI argument/format boundary. These categories are not interchangeable.

The cross-process corpus runner performs 66 checks: six expected acceptances
and 60 expected rejections across both verifiers. Each verifier accepts all
three winning proofs and rejects the three losing proofs. Controlled mutations
cover the header, A leaf bytes, A root, declared tree size, declared M,
truncation, trailing data, invalid base64, and zero target. The official result
must fail at the expected verification/decoding stage; a crash, panic, timeout
or argument failure never substitutes for the intended semantic rejection.

The corpus runner parses the generated proof itself and binds its dimensions,
row indices, leaf indices and commitment roots to independent expected data.
It does not trust labels copied into an export manifest.

All six real Rust `cp_proof_gzip_b64` outputs decompress to the exact original
serialized proof. The raw proofs are 100,432 or 100,464 bytes; gzip is 4,781 to
4,876 bytes **for these sparse synthetic inputs**. These sizes are not a claim
about other signal distributions or accepted pool traffic.

## Build and reproduce

`configs/proof-build.json` defines native Linux x86_64 and Windows x64 MSVC
profiles, using Rust/Cargo 1.99.1, two compile jobs and locked dependencies. The
builder requires C/C++ build tools, CMake 3.24+ and Python 3.11.8+ or 3.12+; the
fixed Windows CI uses Python 3.12. Install Rust through its official rustup
distribution. The builder verifies the exact rustc release/commit/host and both
Cargo.lock hashes. It explicitly sets the Cargo target so an inherited setting
cannot select an old artifact from another output folder. The target must match
the actual host: cross-compiling cannot satisfy the executed unit-test gate.

The recorded Linux command sequence remains:

```sh
git submodule update --init --recursive
rustup toolchain install 1.99.1 --profile minimal
python tools/build_pearl_proof.py
cmake -S . -B build/proof-cmake -DCMAKE_BUILD_TYPE=Release -DNEURO_ENABLE_PROOF=ON
cmake --build build/proof-cmake -j 2
RAYON_NUM_THREADS=2 ctest --test-dir build/proof-cmake --output-on-failure
python tools/check_pearl_proofs.py
```

The source builder extracts only the four required Rust directories from the
pinned donor into `build/proof-source`. The original submodule remains clean.
It refreshes generated source timestamps so Cargo cannot mistake a different
pin with older commit timestamps for an unchanged build. The compiled target
and registry/Git downloads are reused through Cargo's normal cache; lockfiles
and source identity remain checked. This is build caching, unrelated to model
API prompt caching or billing.

The first build needs access to crates.io and the pinned official Git source.
Cargo also fetches an unused CUTLASS submodule from that source; the reference
checker does not compile it. Subsequent cached builds need not download it
again. `--cargo`/`--rustc` accept explicit tool paths; isolated installations
can set `CARGO_HOME` and `RUSTUP_HOME` without changing the user's HOME.

The build writes `artifacts/proof-build.json`, including source inventory,
compiler, target, profile, CRT, commands, lock identities, native dependencies
and artifact hashes. CMake refuses proof mode if the successful report, matching
configuration or real static library is absent. CPU-only builds still work
without Rust when `NEURO_ENABLE_PROOF` is OFF. Unsupported host/target combinations
fail explicitly.

### Windows MSVC profile

`windows/ci.ps1` prepares the x64 Visual Studio environment and configures
Ninja/Release under `build/windows`. It passes the native
`x86_64-pc-windows-msvc` target to the Rust builder. The Rust static library
`cp_proof_ffi.lib` uses the dynamic release CRT, matched by
`CMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL`; target-specific system libraries
come from the actual compiler's `--print native-static-libs` output. The pinned
donor and official source remain unmodified.

From Windows PowerShell or PowerShell 7 with the required tools installed:

```powershell
./windows/ci.ps1 -Action build-cpu
./windows/ci.ps1 -Action run-cpu
```

The build action includes the real Rust unit tests and separately pinned
`pearl-proof-check.exe`. The run action requires matching build fingerprints,
then executes native CPU tests, the cross-process proof corpus and complete
loopback replay. Its reports are under `artifacts/windows/`; Rust stage logs
remain under `artifacts/proof-build/`. The independent fixture regeneration
still uses the separately hash-locked Linux Python environment; Windows native
tests consume the committed expected data.

The complete Windows sequence is a separate validation gate currently in
progress. A successful donor Rust stage alone does not establish successful
reference-checker compilation, native linking or proof/replay execution. See
`docs/windows-worker.md` for prerequisites, CPU/GPU actions and recorded evidence.

`tools/check_pearl_proofs.py` exports fresh corpus files under
`artifacts/proof-corpus`, bounds each verifier process, and saves
`artifacts/proof-check.json`. It selects the native executable paths for Linux
or Windows; `--native` and `--reference` can select explicit built binaries.
The GitHub Linux `proof` job reproduces the build, fixture check, native suite
and cross-process verification. The separate Windows workflow invokes the
fixed Windows actions. Both retain their own logs.

## Host integration

Iteration 5 connects this proof path to the bounded host/backend lifecycle:
receipt of work, immutable job/generation identity, stale-work cancellation,
arithmetic selection, proof construction and actual TCP submission payload.
`docs/host-replay.md` describes the local peer, independent verification of the
received bytes and expected failure cases. It also documents the combined CUDA
executable and its observed CPU-only replay.
The Windows build/run path and bounded SSH worker now exist; their observed
platform and endpoint results must accompany any execution claim. Persistent
zero-B attempts, production-sized scheduling, physical CUDA results, accepted
network shares and live DSH/provider billing remain separate work items.
