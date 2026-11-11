# Windows build and bounded SSH worker

The first target remains the owner's Windows RTX 5080 Laptop. The repository
contains a native MSVC proof build, fixed Windows actions and a Linux SSH
dispatcher. GitHub's Windows job validates CPU/proof/loopback and worker behavior
on its Windows runner. The owner's laptop connection and physical GPU execution
require separate observed results; a Windows CI pass does not establish either.

## Native proof build

`configs/proof-build.json` pins Rust 1.99.1, its compiler commit, both lockfiles
and source commits. It now has native Linux x86_64 and Windows MSVC x64 profiles.
The builder refuses to label a cross-compile as an executed unit-test gate.
Windows uses the release dynamic CRT, matched by CMake's
`CMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL`. CMake checks target, host, profile,
CRT, library hash and configuration hash against the successful build report.

System link libraries are taken from the actual target compiler's
`--print native-static-libs` output. They are not guessed from the Linux list.
The source submodules stay pinned and unmodified. `.gitattributes` preserves
literal LF bytes in Cargo lockfiles, source scripts and fingerprinted C# files.

Required installed tools: x64 Visual Studio C++ Build Tools, Windows SDK,
Python 3.12, Git, CMake 3.24+, Ninja and rustup with the pinned toolchain. A CUDA
build also requires an installed CUDA toolkit supporting SM120 (at least 12.8)
and a host compiler supported by that toolkit. No worker action installs tools
or changes drivers. `inventory` reports what is actually available first.

The fixed entry point locates Visual Studio with `vswhere`, enters its x64
Developer PowerShell environment and uses Ninja/Release under `build/windows`.
The [Microsoft developer-shell documentation](https://learn.microsoft.com/en-us/visualstudio/ide/reference/command-prompt-powershell)
describes that environment. CPU-only proof CI does not require a CUDA toolkit.

The pinned official Pearl checkout recursively fetches CUTLASS, whose example
filenames exceed the legacy Windows path limit under the usual user-profile
Cargo cache. The fixed entry point defaults `CARGO_HOME` to `nc` at the root of
the checkout's drive (for example `C:\nc`). It preserves an explicitly supplied
short writable cache path. The builder checks its length before fetching; the
cache must be writable by the worker account. This does not relocate rustup's
installed toolchains or require changing the registry or global Git settings.

From Windows PowerShell or PowerShell 7 in the checkout:

```powershell
./windows/ci.ps1 -Action build-cpu
./windows/ci.ps1 -Action run-cpu
```

For an actual suitable GPU machine, use this sequence instead:

```powershell
./windows/ci.ps1 -Action build-probes
./windows/ci.ps1 -Action run-cpu
./windows/ci.ps1 -Action run-cuda -Device 0
```

`build-probes` builds the genuine Rust proof library and combined SM120 host;
it does not execute the GPU. `run-cpu` runs Python controls, native CTest, the
66-case proof corpus and complete local TCP replay. `run-cuda` first verifies
the small MMA, full-K and Pearl-input probes, then exercises the same TCP replay
with the explicit CUDA backend. It requires actual successful GPU evidence,
not merely an executable exit code.

Successful build and CPU reports record current source and binary fingerprints.
Changing a checked source or executable invalidates the prerequisite for the
next action. Reports live under `artifacts/windows/`; a new action starts with
an unsuccessful report marker, so interruption cannot leave the previous success
standing in for the current run. These remain synthetic correctness checks,
not continuous production mining or performance comparisons.

## Process ownership and time bounds

`windows/BoundedProcess.cs` starts a process suspended, assigns its Windows Job
Object at creation and then resumes it. The job uses `KILL_ON_JOB_CLOSE`, and
only the intended stdout/stderr/NUL handles are inherited. Deadlines and final
cleanup apply to the owned descendants, including a child which outlives its
parent. Both output pipes are drained with fixed retained byte limits.
See the [Microsoft Job Object contract](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

Each fixed action runs under this outer job. Internal Python tools retain their
own subprocess limits; their Windows `taskkill` fallback alone does not promise
containment after a process leader exits. The outer Job Object supplies that
boundary. No action kills unrelated miners or processes. An OS or driver hang
which stops Windows itself cannot be recovered by an SSH watchdog.

The C# self-test starts real Windows child processes to check exit propagation,
literal arguments, both output pipes, output truncation, timeout and descendant
cleanup after both timeout and normal parent exit. The separate worker
integration test uses actual inventory and clearly labelled synthetic entry
scripts for success/failure/timeout cases. Those fixtures establish worker
behavior; they are not mining proofs or GPU runs.

## Linux controller and private configuration

Keep an owner-specific profile in ignored `configs/remote.local.json`, based on
`configs/remote.example.json`. Resolve the owner's actual connection description
before supplying the exact identity file, port, known_hosts and Windows paths.
A description path is not automatically a usable private-key filename.

```bash
python tools/remote_worker.py --profile configs/remote.local.json inventory
python tools/remote_worker.py --profile configs/remote.local.json build-probes
python tools/remote_worker.py --profile configs/remote.local.json run-cpu
python tools/remote_worker.py --profile configs/remote.local.json run-cuda --device 0
```

The current controller runs on Linux and requires OpenSSH 9+ for SFTP-based
`scp`. The Windows checkout must already be present and its submodules populated.
The controller stages only its worker, runner and structured request into a
unique work directory. Source distribution and the complete handoff archive
remain part of the later packaging work.

The SSH invocation uses argv without a local shell, no agent forwarding,
`BatchMode`, explicit key/known_hosts files and `StrictHostKeyChecking=yes`.
The key contents are never read into Python or stored in a report. A missing
trusted host-key entry is a connection failure; the worker does not silently
trust a new server. Use the owner's existing verified configuration.

Windows paths containing spaces are handled explicitly. Repository paths go in
structured JSON and quoted PowerShell literals; transport staging paths follow
the narrower documented profile. PowerShell invocation uses UTF-16LE
EncodedCommand. Native executable arguments use Win32 quoting without cmd.exe.

## Reports, stale files and lost SSH

Each request has a unique ID and hashes of the worker, runner and action entry
point. Before build/run the remote checkout must contain the expected entry
point and runner bytes. The controller accepts only the matching report and
consistent outcome fields. `inventory` may inspect a missing checkout without
pretending a build action ran.

The wrapper distinguishes passed, failed, timeout, output limit and worker error.
Process success is only one prerequisite: the fixed entry point also enforces
the native proof and correctness gates. Local SSH/scp diagnostics and argv with
private connection references are not copied into public evidence.

After a lost SSH session, the controller makes one bounded attempt to fetch the
existing result. It never blindly resubmits the action. No valid matching report
means remote execution is **unknown**. A retrieved success report with an
inconsistent or timed-out transport is retained as evidence, while the dispatch
as a whole is not marked successful. Reconnection or inspection must resolve
that uncertainty before another GPU run is launched.

The owner provided access information in iteration 6 for the final DSH directive.
The private note `neuro-owner-handoff-iteration06.json` retains those details.
Neither it nor private keys belong in the public source archive. The final
directive should reference the existing key location on the owner VM and carry
the owner-specific connection instructions separately.

## DSH integration boundary

MiniMax M3 is the owner-selected second trial. Invoke the existing DSH through
its actual available interface. `docs/model-selection.md` specifies the durable
budget hooks and recovery behavior; `neuroctl budget` provides executable hooks
without inventing a DSH transport or making a paid API request.

The Windows worker and budget ledger are small integration components. They do
not establish a configured live DSH session, an authenticated laptop connection,
verified provider billing or competitive hashrate. Retain those distinct states
when preparing the iteration-10 archive and directive.
