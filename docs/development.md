# Build and verification entry point

The current project state is `state/current.json`. The root README is intentionally
zero bytes. Run commands from the repository root; use the recorded pinned
submodule rather than an upstream moving branch.

## Offline CPU work

Python 3.11+, C11 and C++17 compilers are sufficient for the Python/native checks.
The direct `cpu-check` recipe and two donor arithmetic differentials currently
require GCC on Linux. CMake 3.24+ also builds the portable native targets.

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

For Windows, run configuration in an x64 Visual Studio developer environment
with a compatible CUDA toolkit. Multi-configuration generators put executables
under `build/cuda/Release/` and append `.exe`. Windows compilation/execution
remains untested here. See `docs/fullk-kernel.md` for bounded physical GPU checks.

## Real proof build and separate official verifier

The iteration-4 optional proof build requires Linux x86_64, Rust/Cargo 1.99.1,
and Python 3.11.8+ or 3.12+. The complete contract and limitations are in
`docs/proof.md`; dependency origins are in `docs/proof-provenance.md`.

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
The Linux native CTest set now has thirteen entries with proof mode enabled.
Ordinary ten-test CPU builds remain independent of Cargo.

The corpus check generates six proofs and performs 66 checks across the native
FFI and official verifier, including expected losing tiles and structured
corruptions. It checks the actual Rust gzip payload as well. No command in this
section connects to a pool or invokes a GPU. The proof link recipe has not been
validated on Windows.

With the previously installed hash-locked Python fixture binding, check the
independent jackpot/target expectations separately:

```bash
python tools/generate_proof_fixtures.py --check
```

The builder and checks save JSON evidence under `artifacts/`. The GitHub proof
job runs the same workflow and uploads the logs. Initial dependency download is
required; subsequent builds reuse Cargo's cache.

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
component tests. The Python replay is an additional process-level gate and is
also run by GitHub's proof job. Native failure in an expected disconnect or
ACK-timeout scenario counts as a replay pass only when its exact unresolved
result and zero accepted/rejected counters are observed.

## Evidence and fixture changes

`reports/iteration-05-validation.json` records the checked source hashes and
coverage. Reports from earlier iterations describe their historical inputs.

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
