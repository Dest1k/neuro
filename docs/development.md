# Build and verification entry point

The current project state is `state/current.json`. The root README is intentionally
zero bytes. Run commands from the repository root; use the recorded pinned
submodule rather than an upstream moving branch.

## Offline CPU work

Python 3.11+ and a C++17 compiler are sufficient for the Python/native checks.
The direct `cpu-check` recipe and two donor arithmetic differentials currently
require GCC on Linux. CMake 3.24+ also builds the portable native targets.

```bash
git submodule update --init --recursive
python tools/neuroctl.py source-check
python -m unittest discover -s tests -v
python tools/generate_fullk_fixtures.py --check
python tools/neuroctl.py cpu-check --output artifacts/cpu-check.json
```

The last command builds and runs six checks: donor CLI, donor pool message
parsing, scalar milestone math, MMA register layout, full-K fixed vectors, and
the full-K donor differential. It uses no GPU, pool connection or proof stub.

Alternative native build:

```bash
cmake -S . -B build/cmake -DCMAKE_BUILD_TYPE=Release
cmake --build build/cmake -j 2
ctest --test-dir build/cmake --output-on-failure
```

GCC/Linux has six CTest entries. Other platforms omit the two donor arithmetic
differentials, which use a GCC LTO link recipe. A smaller test count on those
platforms does not establish the omitted coverage.

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

This creates `neuro_mma_probe` and `neuro_fullk_probe`. Ordinary CTest only
exercises the new probe's host argument validation; it does not start GPU work.
An unavailable `nvcc` must be supplied through `CMAKE_CUDA_COMPILER` or PATH.
Do not silently compile for a different device architecture.

For Windows, run configuration in an x64 Visual Studio developer environment
with a compatible CUDA toolkit. Multi-configuration generators put executables
under `build/cuda/Release/` and append `.exe`. Windows compilation/execution
remains untested here. See `docs/fullk-kernel.md` for bounded physical GPU checks.

## Evidence and fixture changes

`reports/iteration-02-validation.json` records the checked source hashes and
coverage. Reports from earlier iterations describe their historical inputs.

`tools/generate_fullk_fixtures.py` defaults to read-only verification. Its
`--write` option deliberately changes expected data; use it only after reviewing
a change to the input/oracle contract. A candidate mismatch is a reason to debug
the candidate, not regenerate matching expected output. The generator uses only
Python's standard library and does not invoke the native implementation.

The SHA-256 manifest describes synthetic arithmetic vectors, not Pearl jobs or
valid proofs. Native libraries can be reused by later adapters without importing
the fixture generator into production GPU work.
