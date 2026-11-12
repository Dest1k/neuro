# Build and verification entry point

GitHub Actions is disabled for this project after the account-suspension
incident. Run the commands below directly in the local development environment
or on the owner's machines; the archived workflow files are not execution
entry points. See `docs/incidents/2026-11-12-github-suspension.md`.

The current project state is `state/current.json`. The root README is intentionally
zero bytes. Run commands from the repository root; use the recorded pinned
submodule rather than an upstream moving branch.

For a newly extracted handoff archive, start with `docs/handoff-runbook.md`.
It specifies the first exact-package verification, Linux prerequisite inventory,
fresh builds, source transfer to Windows and the remaining real DSH accounting
integration. The flat archive already contains initialized pinned source and
portable Git metadata; it needs no initial submodule download. The command below
is for an ordinary Git clone whose submodules have not been initialized.

## Offline CPU work

The basic Python checks need Python 3.11+ and Git. Basic native checks additionally
need C11 and C++17 compilers. The direct `cpu-check` recipe and three donor
arithmetic differentials currently require GCC on Linux. The CMake route needs
CMake 3.24+, matching CTest and a generator build tool: GNU Make for the default
Unix Makefiles recipe, or Ninja when explicitly selected. Rust and the optional
Python BLAKE3 fixture binding are separate dependencies described below.

```bash
git submodule update --init --recursive
python tools/neuroctl.py source-check
python -m unittest discover -s tests -v
python tools/generate_fullk_fixtures.py --check
python tools/generate_blake3_vectors.py --check
python tools/neuroctl.py cpu-check --output artifacts/cpu-check.json
```

The last command builds four C objects and runs ten checks: the previous six
CLI/protocol/arithmetic checks, official BLAKE3 vectors, donor alignment and
cancellation, Pearl input fixed vectors, and the real-input donor differential.
It uses no GPU, pool connection or proof stub. The pinned portable BLAKE3 library
and donor noise code build from source; Cargo is not needed for these targets.

Alternative native build:

```bash
cmake -S . -B build/cmake -DCMAKE_BUILD_TYPE=Release
cmake --build build/cmake -j 2
ctest --test-dir build/cmake --output-on-failure
```

GCC/Linux has ten CTest entries. Other platforms omit the three donor arithmetic
differentials, which use a GCC LTO link recipe. A smaller test count on those
platforms does not establish the omitted coverage.

Regenerating/checking the independent Pearl expected data additionally needs
the hash-locked official Python BLAKE3 binding. The committed C++ checks consume
fixed vectors and do not import this Python package. For the verified Linux
x86_64 CPython 3.11/3.12 regeneration environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-fixtures.txt
python tools/generate_pearl_fixtures.py --check
```

The lock contains wheels for those two Python/platform combinations. Other
combinations need a reviewed wheel hash update; do not remove the hash check.

## CUDA component build

Use a CUDA toolkit supporting SM120 and its supported host compiler. The
recorded Linux build uses CUDA 13.5.37 and GCC 14.2.0. CUDA 12.8+ is the CMake
minimum; other toolkit/OS combinations have not been verified in this project.

```bash
cmake -S . -B build/cuda -DNEURO_ENABLE_CUDA=ON \
  -DNEURO_BUILD_CPU_CHECKS=OFF -DCMAKE_CUDA_ARCHITECTURES=120 \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/cuda --config Release -j 2
ctest --test-dir build/cuda -C Release --output-on-failure
```

This creates `neuro_mma_probe`, `neuro_fullk_probe`, and `neuro_pearl_probe`.
Ordinary CTest exercises full-K host argument validation and the Pearl consumer's
host preparation/reference path (two tests). It does not start GPU work.
An unavailable `nvcc` must be supplied through `CMAKE_CUDA_COMPILER` or PATH.
Do not silently compile for a different device architecture.

For Windows, use the fixed MSVC/Ninja recipe below. It enters the x64 Visual
Studio environment and puts the release executables in `build/windows/` with
the `.exe` suffix. Its `build-probes` action includes the real proof and CUDA
host. A compiled executable and a passing CPU backend do not establish physical
GPU execution. See `docs/fullk-kernel.md` for bounded component checks and
`docs/windows-worker.md` for the complete Windows action sequence.

## Real proof build and separate official verifier

The optional proof builder has native Linux x86_64 and Windows x64 MSVC profiles,
pinned to Rust/Cargo 1.99.1. It requires Python 3.11.8+ or 3.12+ and refuses to
present cross-compiled unit tests as executed tests. The following recipe is
the recorded Linux path; the Windows entry point is below. The complete contract
and limitations are in `docs/proof.md`; dependency origins are in
`docs/proof-provenance.md`.

```bash
rustup toolchain install 1.99.1 --profile minimal
python tools/build_pearl_proof.py
cmake -S . -B build/proof-cmake -DCMAKE_BUILD_TYPE=Release -DNEURO_ENABLE_PROOF=ON
cmake --build build/proof-cmake -j 2
RAYON_NUM_THREADS=2 ctest --test-dir build/proof-cmake --output-on-failure
python tools/check_pearl_proofs.py
```

This builds actual `cp-proof-ffi` in a generated source copy, executes its 20
unit tests, and builds an official Pearl verifier from a separate pinned Git
dependency. CMake's proof option checks the successful build report and static
library hash. Missing real Rust output is an error; no stub is available.
The Linux native CTest set now has fifteen entries with proof mode enabled,
including bounded attempt recovery and the durable journal.
Ordinary ten-test CPU builds remain independent of Cargo.

The corpus check generates six proofs and performs 66 checks across the native
FFI and official verifier, including expected losing tiles and structured
corruptions. It checks the actual Rust gzip payload as well. No command in this
section connects to a pool or invokes a GPU. Windows uses its own target,
dynamic release CRT, native link dependencies and separately recorded gates.

With the previously installed hash-locked Python fixture binding, check the
independent jackpot/target expectations separately:

```bash
python tools/generate_proof_fixtures.py --check
```

The builder and checks save JSON evidence under `artifacts/`. Run them directly
in the local environment or on owner hardware. The Windows entry point is
`windows/ci.ps1`; its filename does not authorize execution in GitHub Actions.
Initial dependency download is required; subsequent builds
reuse Cargo's cache. A cached file does not replace a successful current build
report matching the source/configuration and library hash.
The initial download includes the official Pearl Git dependency and its CUTLASS
submodule as well as locked registry crates. They are not bundled in the source
handoff archive. Set `CARGO_NET_OFFLINE=true` only when the required sources are
already cached; a missing cache entry must remain a build failure. Do not copy
an old `artifacts/proof-build.json` or compiled library into a fresh checkout.

## Windows build and bounded remote actions

The implemented Windows recipe needs x64 Visual Studio C++ Build Tools, Windows
SDK, Python 3.12, Git, CMake 3.24+, Ninja and the pinned Rust toolchain already
installed. `windows/ci.ps1` finds Visual Studio with `vswhere` and configures
Ninja/Release with `MultiThreadedDLL` to match Rust. It does not install tools
or change drivers. From Windows PowerShell or PowerShell 7 in the checkout:

```powershell
./windows/ci.ps1 -Action build-cpu
./windows/ci.ps1 -Action run-cpu
```

`build-cpu` builds the real donor Rust library and separate official checker,
then the native CPU/proof/host targets. `run-cpu` requires matching build evidence
and runs Python controls, native CTest, the 66-case proof corpus and complete
19-scenario loopback replay, including cumulative attempt continuation, followed
by the separate process-restart gate in `tools/replay_recovery.py`. The latter
checks interrupted and uncertain outcomes, retained work progress, encoding
changes and exclusive journal ownership with real independently verified proofs.
Windows omits the three GCC/Linux donor arithmetic
differentials. Iteration 6 passed its then-current CPU/proof/loopback sequence
on an actual Windows runner. Changed iteration-7/8 gates remain unrun on Windows.
`reports/iteration-06-validation.json` records that historical tested commit,
per-platform counts and CI URL. Owner-laptop and GPU execution remain separate.

On the owner's suitable SM120 GPU machine, after installing a compatible CUDA
toolkit and supported MSVC host compiler, use:

```powershell
./windows/ci.ps1 -Action build-probes
./windows/ci.ps1 -Action run-cpu
./windows/ci.ps1 -Action run-cuda -Device 0
```

`build-probes` adds the CUDA kernels/host but does not launch them. `run-cuda`
requires a matching successful CPU report, checks exact results from the three
physical probes, then runs the same TCP replay with `--backend cuda`. Reports
under `artifacts/windows/` bind source and binary fingerprints to the action.
Changes invalidate the next gate's prerequisite. A Windows CPU CI result does
not demonstrate the owner's laptop connection, GPU execution or performance.

The Linux controller accepts a private profile based on
`configs/remote.example.json`. Use the owner's verified SSH information in the
ignored local file; do not put credentials in the repository or these commands:

```bash
python tools/remote_worker.py --profile configs/remote.local.json inventory
python tools/remote_worker.py --profile configs/remote.local.json build-probes
python tools/remote_worker.py --profile configs/remote.local.json run-cpu
python tools/remote_worker.py --profile configs/remote.local.json run-cuda --device 0
```

These commands are for a configured real endpoint. They are not evidence that
SSH has already connected. The worker uses fixed actions, bounded output and
process lifetime, strict request identity and a Windows Job Object for owned
descendants. An interrupted transport gets one bounded result fetch, without
blindly rerunning the action. Source transfer, full packaging and a completely
hung operating system are outside this worker's recovery contract. Details and
the private handoff boundary are in `docs/windows-worker.md`.

## Host lifecycle and real loopback replay

After the preceding proof build:

```bash
python tools/replay_pool.py --output artifacts/pool-replay.json
```

This launches a real native TCP client and a local Python peer. It exercises
generation changes, cancellation during computation/proof, reconnects, malformed
messages and exact acknowledgement accounting. Actual submitted PlainProofs
are decoded and checked in the separate official verifier. All jobs and wallet
labels are synthetic; no external pool connection is made. The default backend
is CPU. `docs/host-replay.md` specifies the supported donor wire dialect and
provides combined CUDA/proof build commands and the explicit hardware-run mode.

An ordinary proof-enabled native CTest run includes the coordinator and wire
component tests. The Python replay is an additional process-level gate available
on both platforms. Native failure in an expected disconnect or
ACK-timeout scenario counts as a replay pass only when its exact unresolved
result and zero accepted/rejected counters are observed.

The client now accepts an explicit bounded attempt sequence. Read
`docs/attempt-search.md` for the cumulative row mutation rule, preserved
attempt-zero vectors and the new miss/continuation/cancellation cases. Verify
their separate expected values with
`python tools/generate_attempt_fixtures.py --check` after installing the pinned
fixture binding. The default replay still uses one attempt unless a scenario
explicitly requests more; this keeps historical checks comparable.

For remote Windows GPU checks, read `docs/windows-worker.md`,
`docs/candidate-identity.md` and `docs/gpu-miner-handoff.md`. Configure the owner
miner privately before `run-cuda`; build and CPU checks do not stop it.
Candidate manifests cover the complete public checkout, so finish source and
documentation edits before a build/CPU/GPU gate sequence.

## Durable bounded-search recovery

After the real proof/native build, run the additional fresh-process gate:

```bash
python tools/replay_recovery.py --output artifacts/search-recovery.json
```

Use `--native PATH` and `--reference PATH` to select a different build. The
default backend is CPU; a linked CUDA executable with `--backend cpu` still
provides CPU evidence only. No owner miner is stopped for these checks.

`docs/search-recovery.md` specifies journal identity, flush ordering and the
conservative interrupted/uncertain policy. `--journal PATH` requires an existing
parent directory and keeps the same peer scope/root seed across restart. Do not
delete or truncate it to repeat consumed attempts. Runtime journals belong
under ignored generated storage, not in public source or final handoff archives.

## Local model budget boundary

MiniMax M3 is the owner-selected retrial executor; Sol is a disabled-by-default,
explicit bounded fallback. `neuroctl budget` provides durable reserve, begin,
reconcile, cancel and status commands. It holds full-price request bounds before
dispatch, keeps unresolved charges across process restarts and applies shared
session/campaign limits without speculative cache discounts. See
`docs/model-selection.md` for executable CLI examples and the caller contract.

These commands do not send an API request or launch DSH. A real adapter must
apply the declared input/output bounds and route every paid attempt through the
gate. Provider cache/billing verification and observed DSH integration remain
separate work; synthetic usage tests do not establish either.

## Evidence and fixture changes

Use `state/current.json` and the latest `docs/iterations/` entry to locate the
current validation report. `reports/iteration-05-validation.json` and earlier
reports preserve the source hashes and results of their historical inputs;
they are not interchangeable with the new Windows or budget gates.

`tools/generate_fullk_fixtures.py` defaults to read-only verification. Its
`--write` option deliberately changes expected data; use it only after reviewing
a change to the input/oracle contract. A candidate mismatch is a reason to debug
the candidate, not regenerate matching expected output. The generator uses only
Python's standard library and does not invoke the native implementation.

`generate_pearl_fixtures.py` follows the same check-by-default rule. Its separate
manifest records synthetic jobs passed through the protocol preparation formulas.
The jobs are not network-issued work. Their input checks alone do not establish
a valid proof; the separate iteration-4 proof suite supplies that evidence for
the selected fixtures.
`generate_blake3_vectors.py` copies published expected hashes from the pinned
official JSON; it never derives answers by calling the C library under test.
Native libraries can be reused without importing either generator into GPU work.
