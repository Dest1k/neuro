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

## Evidence and fixture changes

`reports/iteration-03-validation.json` records the checked source hashes and
coverage. Reports from earlier iterations describe their historical inputs.

`tools/generate_fullk_fixtures.py` defaults to read-only verification. Its
`--write` option deliberately changes expected data; use it only after reviewing
a change to the input/oracle contract. A candidate mismatch is a reason to debug
the candidate, not regenerate matching expected output. The generator uses only
Python's standard library and does not invoke the native implementation.

`generate_pearl_fixtures.py` follows the same check-by-default rule. Its separate
manifest records synthetic jobs passed through the protocol preparation formulas.
The jobs are not network-issued work and do not establish a valid proof.
`generate_blake3_vectors.py` copies published expected hashes from the pinned
official JSON; it never derives answers by calling the C library under test.
Native libraries can be reused without importing either generator into GPU work.
