# Fresh checkout and owner-machine handoff

This is the executable preparation runbook for the flat source package. The
iteration-10 directive will add the owner's private connection references and
the final measured state. Read `AGENTS.md`, `state/current.json`, the latest
`docs/iterations/` entry and `reports/iteration-09-validation.json` when present
before choosing new miner work. The root README stays empty.

Use the owner's Linux x86_64 VM for orchestration and CPU work. Windows is a
separate native build and physical GPU gate. Source verification, compilation,
CPU replay, physical GPU correctness, accepted pool shares and actual model
billing are separate observations; one does not establish the next.

## 1. Verify the extracted package first

Before extraction, compare the ZIP's SHA-256 with the value delivered separately
with that archive. Its internal manifest detects changed payloads but cannot
authenticate a wholly replaced archive. See `docs/handoff-package.md` for the
package format and verification boundaries.

Extract the archive into an **empty** `/home/jericho/neuro/start/`. Its root
contains `AGENTS.md`, `tools/`, `native/`, populated `upstream/` directories and
portable Git metadata; there is no extra `neuro/` directory or restore step.
The archive contains sources and historical reports, not a prebuilt miner,
installed compilers, Cargo dependency caches or private configuration.

Run the package verifier immediately after extraction, before builds or other
Git commands which can refresh the index:

```bash
cd /home/jericho/neuro/start
python3 -B -s -E _work/handoff/verify.py verify --root .
```

It checks the packaged files against `_work/handoff/MANIFEST.json` and its
checksum, then checks actual source identity, pinned dependencies, the empty
README and absence of active Actions. A mismatch must be resolved before using
the package. After intentional edits, the original exact-package comparison no
longer describes the working tree: use `source-check` and a new candidate
manifest for the edited source.

The package has no active Git remotes. Local development and verification do
not require a GitHub account or publication. No command in this runbook uses
GitHub Actions, including self-hosted runners. Keep archived workflows disabled
across future clones and remotes; see `docs/incidents/2026-11-12-github-suspension.md`.

If an earlier execution already has a model-budget database, search journal or
GPU lease journal, retain that state and its identifiers. A new source checkout
does not authorize a new budget or replay of consumed work. These runtime files
are deliberately absent from distributable source archives.

## 2. Inventory the Linux VM and run the basic checks

The inventory command is for the Linux x86_64 VM. It checks installed
prerequisites for a selected stage; it does not install tools, download
dependencies, build code, contact the laptop, call a model or execute a GPU.
Its successful result means the selected prerequisites were found. It does not
mean that compiler compatibility or any miner test has passed.

```bash
python3 tools/handoff_preflight.py --stage basic \
  --output artifacts/handoff-preflight-basic.json
python3 tools/neuroctl.py source-check
python3 -m unittest discover -s tests -v
python3 tools/generate_fullk_fixtures.py --check
python3 tools/generate_blake3_vectors.py --check
```

Basic inventory requires Python 3.11+, Git, a C11/C++17 toolchain, CMake 3.24+,
matching CTest and GNU Make for the default Unix Makefiles generator. The
recorded complete Linux check set uses GCC; three donor differentials and
`neuroctl cpu-check` depend on GCC/Linux. A different compiler can omit those
checks and must not be reported as the same coverage.

Build and execute the basic native checks with the recorded generator:

```bash
cmake -S . -B build/cmake -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
cmake --build build/cmake --parallel 2
ctest --test-dir build/cmake --output-on-failure --no-tests=error
```

For an existing Ninja installation, select `--generator Ninja` in preflight and
`-G Ninja` in CMake, using a fresh build directory for a changed generator.
`--build-tool` selects an explicit Make/Ninja binary for inventory. CMake needs
the same executable on PATH or an explicit `-DCMAKE_MAKE_PROGRAM=...`.

An inventory failure should produce a concrete missing-tool list. Locate or
install the missing dependency in the owner's environment before rerunning that
stage. Do not lower a version pin, remove a test or substitute a proof stub to
make inventory or compilation pass.

### Non-default installed tool locations

Preflight accepts `--cmake`, `--ctest`, `--cc`, `--cxx`, `--cargo`, `--rustc`,
`--cargo-home` and `--nvcc`. These options select what it inspects; they do not
change another command's PATH or configuration. Propagate the same selections:

| Inventory selection | Subsequent command must use |
|---|---|
| `--cmake`, `--ctest` | Those exact executables, or their installed directory on PATH |
| `--cc`, `--cxx` | CMake `CMAKE_C_COMPILER` and `CMAKE_CXX_COMPILER` |
| `--generator`, `--build-tool` | CMake `-G` and `CMAKE_MAKE_PROGRAM`, or the selected build tool on PATH |
| `--cargo`, `--rustc`, `--cargo-home` | The same named options to `tools/build_pearl_proof.py` |
| `--nvcc` | CMake `CMAKE_CUDA_COMPILER`, or the selected toolkit's `bin` on PATH |

Use actual paths discovered on this machine. Paths from historical reports
under a web-session workspace are provenance, not executable owner-VM paths.

## 3. Build the genuine proof and run the complete CPU gates

The proof stage additionally requires native Linux x86_64, Python 3.11.8+ or
3.12+, and the exact Rust/Cargo 1.99.1 toolchain and compiler commit pinned in
`configs/proof-build.json`. Its inventory resolves already installed local
rustup toolchain binaries without triggering toolchain downloads. If missing,
the separate installation step is:

```bash
rustup toolchain install 1.99.1 --profile minimal
```

The pinned recipe rejects inherited `RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS`,
`RUSTC_WRAPPER` and `RUSTC_WORKSPACE_WRAPPER`. Clear any such override for this
build. Preflight reports their names without serializing environment values.

Dependency acquisition is a separate network operation. The first real proof
build needs the locked registry crates and the pinned official Pearl Git
dependency; Cargo also checks out its CUTLASS submodule. The source package does
not bundle those Cargo caches. On a machine where they already exist,
`CARGO_NET_OFFLINE=true` requests an offline build and fails if a dependency is
missing. A preflight pass does not prove the Cargo cache is complete.

```bash
python3 tools/handoff_preflight.py --stage proof \
  --output artifacts/handoff-preflight-proof.json
python3 tools/build_pearl_proof.py
cmake -S . -B build/proof-cmake -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
  -DNEURO_ENABLE_PROOF=ON
cmake --build build/proof-cmake --parallel 2
RAYON_NUM_THREADS=2 ctest --test-dir build/proof-cmake \
  --output-on-failure --no-tests=error
python3 tools/check_pearl_proofs.py --output artifacts/proof-check.json
python3 tools/replay_pool.py --backend cpu --output artifacts/pool-replay.json
python3 tools/replay_recovery.py --backend cpu --output artifacts/search-recovery.json
```

Use the default proof report `artifacts/proof-build.json` for this sequence.
The builder replaces it with an unsuccessful in-progress marker before doing
work and records actual static-library/reference-checker hashes on success.
Its `--output` option alone does not repoint downstream CMake or checker
commands. Copying an old report or binary from a prior checkout is not a fresh
build. The proof source copy and targets are generated below `build/` in the
current checkout; the pinned reference submodules must remain clean.

The cross-process corpus checks six genuine proofs with 66 acceptance/rejection
checks. TCP replay and durable restart recovery use local synthetic peers and
the independent official verifier. Their CPU results do not show an external
pool connection or a working CUDA backend. Keep process-restart journals when
investigating a real interrupted run; see `docs/search-recovery.md`.

### Independent fixture regeneration checks

The committed native tests use fixed expected data and need no Python BLAKE3
package. To independently recompute Pearl, proof and attempt expectations, use
the hash-locked binding in a separate Linux CPython 3.11/3.12 environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --require-hashes -r requirements-fixtures.txt
python3 tools/handoff_preflight.py --stage basic --require-fixtures \
  --output artifacts/handoff-preflight-fixtures.json
python3 tools/generate_pearl_fixtures.py --check
python3 tools/generate_proof_fixtures.py --check
python3 tools/generate_attempt_fixtures.py --check
```

The lock contains reviewed Linux x86_64 wheels for those two Python versions.
It is not a Windows fixture-install recipe. Do not remove hashes, change gold
files or use a candidate's own output as expected data to repair a mismatch.
`--require-fixtures` checks CPython/version, package metadata and an actual
bounded import with the official empty-input hash. That confirms a usable
binding; the inventory does not independently verify the installed wheel hash.

## 4. Optional Linux CUDA compilation

The owner's physical target is SM120. This stage adds a CUDA compiler supporting
that architecture, at least 12.8, to the proof prerequisites. The recorded Linux
combination is CUDA 13.5.37 with GCC 14.2.0; inventory is not a compatibility
guarantee for another host-compiler/toolkit pair.

```bash
python3 tools/handoff_preflight.py --stage cuda-build \
  --output artifacts/handoff-preflight-cuda-build.json
```

Use the combined proof/CUDA build commands in `docs/host-replay.md` when this
compiler stage is useful. Neither `nvcc` inventory nor compilation requires
stopping SRBMiner. A linked CUDA binary or CPU execution of that binary still
does not establish GPU correctness. The primary next hardware result is the
current native Windows sequence below, not more Linux compile repetitions.

## 5. Observe the Windows environment and preposition the same source

Resolve the owner's existing connection description privately. Populate ignored
`configs/remote.local.json` from `configs/remote.example.json` with the actual
host, local Windows account, port, usable identity file, already verified
`known_hosts`, checkout directory and worker directory. A connection-description
file is not necessarily the key itself. The Linux controller requires OpenSSH
9+ for SFTP-based `scp`; it does not enroll host keys or inherit arbitrary SSH
config/proxy commands.

```bash
python3 tools/remote_worker.py --profile configs/remote.local.json inventory
```

Inventory works before the Windows checkout exists. It reports available
executables/versions and a checkout HEAD when present. It does not establish
compiler/SDK readiness, a matching Rust pin, a usable desktop, miner ownership,
or permission to stop mining.

Use the existing authenticated owner connection to transfer the same verified
source archive and extract it into a fresh short local Windows directory, for
example `C:\neuro`. The fixed dispatcher transfers only its bounded worker and
request; it does not deploy source. Run the package verifier there before other
Git commands, from a Windows shell in that directory:

```powershell
python -B -s -E _work/handoff/verify.py verify --root .
python tools/neuroctl.py source-check
```

Keep the entire public candidate identical on the VM and Windows between
build/CPU/GPU actions. A plain source copy without its pinned Git metadata is
insufficient. Private local profiles and generated artifacts are excluded from
candidate identity; public source and documentation edits invalidate the build
prerequisite. Finish edits first, then rebuild and rerun gates. See
`docs/candidate-identity.md`.

Required Windows tools are x64 Visual Studio C++ Build Tools, Windows SDK,
`vswhere`, Python 3.12, Git, CMake 3.24+, Ninja and the pinned Rust toolchain. CUDA
work also needs an installed SM120-capable toolkit with a supported MSVC host
compiler. `windows/ci.ps1` enters the x64 developer environment and matches Rust's
dynamic release CRT. No worker action installs tools or changes drivers.

The official Pearl dependency's long filenames require a short writable
`CARGO_HOME`: the fixed Windows entry point defaults to `nc` at the checkout
drive root, such as `C:\nc`; the builder enforces at most eight UTF-16 units.
This cache selection does not move rustup's installed toolchains. Read
`docs/windows-worker.md` before using the Windows entry points.

If CUDA is installed, the existing controller sequence is:

```bash
python3 tools/remote_worker.py --profile configs/remote.local.json build-probes
python3 tools/remote_worker.py --profile configs/remote.local.json run-cpu
```

These actions build genuine proof/host/CUDA code and run current CPU controls,
proof corruption, TCP and restart-recovery checks. They do not execute a GPU or
stop SRBMiner. If CUDA is absent, the CPU-only native entry point is available
directly in a Windows shell:

```powershell
./windows/ci.ps1 -Action build-cpu
./windows/ci.ps1 -Action run-cpu
```

The Linux dispatcher currently has no `build-cpu` action. Do not invent one.
The iteration-6 Windows result belongs to an older source revision; it cannot
replace execution of the current scripts and candidate.

## 6. Establish actual model accounting before a paid autonomous campaign

The owner selected `minimax/minimax-m3` for a bounded retrial. The repository
implements durable local accounting in `tools/neuroctl/budget.py`, not the DSH
model transport. Filling `configs/local.json` or executing `budget reserve`
does not intercept a running DSH session. The actual endpoint, API format,
provider usage semantics, cache billing and installed DSH interface remain
unobserved. Existing DSH startup calls are not retroactively covered by this
ledger either.

Inspect the installed DSH's supported client/hook interface and the configured
provider locally. Connect a small adapter at the **actual paid dispatch point**:

1. Count/bound the fully serialized model input, including system context,
   tool descriptions and results; enforce the reserved full output bound,
   including billable reasoning.
2. Use `BudgetLedger.from_project(...)`, `reserve(...)` and one successful
   `begin_dispatch(...)` before exactly one paid attempt. Every SDK retry must
   have its own reservation or be disabled.
3. Reconcile the final complete usage, using an explicitly verified API format.
   Missing/partial usage retains the hold and blocks more dispatches; keep the
   same database, campaign and session after a process restart.
4. Observe a bounded real call and reconcile its usage with provider billing
   before claiming the adapter or any cache discount is verified.

Exact Python and CLI contracts are in `docs/model-selection.md`. The declared
limits are 64,000 input tokens, 8,000 total output tokens, 200 attempts/$5 per
session and $25 for the initial campaign. Accounting uses ordinary input rates;
the user-quoted reseller tariffs are not a measured provider invoice. Sol is an
explicit fallback disabled by default, with its own sublimits inside the total.

If the installed DSH exposes no suitable accounting hook, record that specific
limitation and use only its independently configured bounded session while
implementing the adapter. Do not claim the repository's $5/$25 limits protect
unintercepted DSH calls or start an unbounded autonomous loop. Do not create a
new agent framework, proxy or guessed DSH command in place of inspecting the
existing installation. API credentials and connection details stay outside
public source, reports and packages.

## 7. First physical GPU group and the next useful miner task

After the current Windows build/CPU gates pass, configure ignored
`configs/gpu-owner.local.json` with the real owner's SID, exact miner executable
and hash, and exact restart batch and hash. The final directive provides the
owner-specific connection and visible-batch requirements; the public example
does not guess those values. Inspect the actual launcher chain before a first
GPU group; unknown wrappers must be handled explicitly, never by killing all
processes with a similar name.

The owner requires the configured SRBMiner batch to return in a visible desktop
window after exclusive GPU use. `docs/gpu-miner-handoff.md` defines the lease,
desktop/scheduler preflight, exact process identities, independent watchdog and
separate restoration evidence. A successful SSH/task launch is not proof of a
visible running miner. Build/CPU work leaves it alone; a miner already stopped
at entry stays stopped.

```bash
python3 tools/remote_worker.py --profile configs/remote.local.json run-cuda --device 0
```

Run one fixed correctness group first. The entry point verifies current source,
build and CPU evidence before acquiring the GPU lease. Preserve an unknown
transport or restoration outcome and inspect its existing request/journal;
do not blindly repeat the GPU action or batch launch. Do not change clocks,
power settings, driver/TDR configuration or reset a runtime journal to make a
test succeed.

The first useful handoff milestone is **current Windows CPU/proof/recovery
evidence plus exact physical GPU results with observed visible SRBMiner
restoration**. If a gate fails, fix that concrete failure against independent
expected data and rerun the affected sequence. After that milestone, measure a
bounded comparable baseline before kernel optimization. The implemented host
currently uses local synthetic peers; an external pool adapter and accepted
shares remain future work, not something this startup sequence promises.
