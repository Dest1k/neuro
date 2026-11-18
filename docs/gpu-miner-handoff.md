# GPU checks and the owner's existing miner

The owner requires SRBMiner to be stopped before GPU checks and resumed by
running the existing **guided-setup.bat in a visible owner desktop window** once
the checks release the GPU. CPU checks, compilation and inventory do not pause
mining. The actual owner location and connection details stay in the private
handoff note and target-local configuration.

## Target configuration and entry points

Populate ignored `configs/gpu-owner.local.json` from
`configs/gpu-owner.example.json` on the real Windows machine. Resolve the current
owner SID and the actual SRBMiner executable; retain the existing batch unchanged.
Record the exact SHA-256 of both files. This is a configuration operation for
DSH, not a request for a human to write CUDA or a replacement mining launcher.

The configured executable and batch must share their directory, use explicit
Windows drive paths and avoid reparse points or cmd metacharacters. The fixed
batch basename is `guided-setup.bat`. The configured SID must match the caller
and the active interactive desktop owner. Missing configuration is a failed
precondition for `run-cuda`, never permission to test beside SRBMiner.

`windows/ci.ps1` integrates these functions around the bounded GPU group:

```powershell
. ./windows/gpu_lease.ps1
$lease = Enter-NeuroGpuLease -RepositoryRoot $Repo -Device $Device
try {
    # Only the fixed bounded GPU probes/replay belong here.
} finally {
    $handoff = Exit-NeuroGpuLease -Lease $lease
    # The action requires $handoff.passed, preserving any original test error.
}
```

## Before stopping anything

A global named mutex prevents two project GPU leases on the host. A durable
journal under the current user's LocalApplicationData `neuro/gpu-leases`
prevents an abandoned/unknown lease from being silently replaced.

Preflight validates the executable and batch hashes, exact process path, owner
SID, PID, creation time and desktop session. It accepts at most one configured
miner. A live cmd launcher must invoke the exact configured batch without extra
commands; it is stopped before its miner to prevent an ordinary batch retry loop
from respawning it. An unknown wrapper, a foreign owner, an unexpected child or
multiple miners fails before termination. Explorer/direct launch or a genuinely
absent original parent has no live batch launcher to stop. No process is selected
for termination merely by its name. Native process handles retain identity while
checking and terminating, so a recycled PID cannot redirect a kill.

The copied watcher module and native helper are checked against their candidate
files, and both hashes are bound into the immutable watcher request.

The current narrow launcher recognizer intentionally rejects nested/custom
launch wrappers it cannot establish. DSH must inspect the actual launch chain
and extend its explicit handling if the owner's batch uses one. It must not
work around this by killing every cmd, conhost or process with a similar name.

## Visible restart and process lifetime

A one-shot Task Scheduler **interactive-token** watchdog starts in the owner's
already active desktop session before the miner is touched. No password is
stored and no System/S4U session is used. The watchdog does not open a console.
It confirms that its own thread is attached to window station `WinSta0` and
desktop `Default`, and that `Default` can be opened with `DESKTOP_CREATEWINDOW`.
GPU probes, replay, the baseline recorder and this watchdog stay windowless
(`CreateNoWindow` or `-WindowStyle Hidden`). The fixed restart action is
registered before declaring readiness. A missing desktop or failed scheduler
operation fails preflight while leaving mining untouched.

The only visible window this lease creates is the restored miner. Explicitly
launching inbox conhost for that restart is a per-launch choice. It preserves
the user's default Terminal settings and avoids treating a ConPTY message window
as a visible desktop window. The real restart action runs:

```text
System32\conhost.exe System32\cmd.exe /d /s /c ""<configured guided-setup.bat>""
```

The working directory is the batch directory. It is a separate interactive
scheduled task with no triggers and no execution time limit; idle/battery
transitions do not terminate it. This separates the resumed owner's normal
long-running mining process from the bounded watchdog and SSH action jobs.
The watchdog's completion or time limit must not terminate the resumed miner.

The report distinguishes restart requested, batch launched, visible HWND
observed, configured miner observed and its exact process identity. A successful
restoration requires the miner's cmd ancestry to lead to the newly launched
conhost, with matching owner/session and creation times. A successful task launch
alone does not prove the owner has a visible running miner.

## Completion, failure and SSH loss

Normal completion and GPU test failure both request release through `finally`.
The fixed GPU subprocesses use `BoundedProcess`: its Job Object closes before a
call returns, including failures and timeouts. The watchdog additionally waits
for all fixed GPU test executable paths to disappear and observes a quiet gap
before resuming mining.

The watchdog is started by Task Scheduler outside the SSH worker's Job Object.
If the action process disappears, it checks PID plus creation time, takes over
an abandoned mutex and attempts the same restoration after GPU process cleanup.
The unresolved journal prevents another lease from racing that recovery. It
waits a bounded 930 seconds for release/owner exit, at most 90 seconds for test
processes to disappear, then at most 15 seconds for the new console process and 45 seconds for visible
miner restoration. Normal release waits up to 180 seconds for that result.
Its task has a 20-minute ceiling. It never starts mining alongside known surviving
GPU test processes or repeats an already-issued restart with an uncertain result.

If mining was stopped at entry, it remains stopped. If the original miner never
terminated, it is preserved. A different newly appearing miner prevents a
second batch launch. Only the one owner-configured miner and its established
batch launcher are terminated; unrelated GPU programs are outside this lease.

An unknown result is a failed handoff and blocks another GPU action. Retain the
journal and inspect `request.json`, `ready.json`, `stop-started.json`,
`restart-issued.json`, `release.json` and `result.json`, plus the named scheduler
tasks and exact process identities. Reconcile an existing restart instead of
rerunning the batch blindly. A fully hung OS/driver or a machine reboot is outside
the live SSH/watchdog recovery guarantee. Owner-laptop execution, console
visibility and real GPU release require observations on that machine.

## Validation boundaries

The iteration-7 implementation has **not yet executed under Windows** in this
preparation environment. GitHub publication/CI was unavailable; the iteration
report records that external block. The existing iteration-6 Windows pass does
not validate these new scripts. The tests below are implemented and ready to run;
neither their successful execution nor owner-desktop restoration is claimed.

`windows/test_gpu_lease.ps1` compiles a tiny non-mining fixture executable and
uses real Windows process identities/termination. Mandatory cases cover changed
files, foreign/recycled identities, exact launcher recognition, overlap denial,
actual stop/restore through an explicitly synthetic desktop adapter, failure
cleanup and preservation of stopped-at-entry state. They do not run SRBMiner or
CUDA and do not establish the owner's desktop visibility.

With `-InteractiveSmoke`, the same fixture additionally attempts the actual
Task Scheduler/visible-console path when the CI account has one active owner
session; absence is explicitly skipped. Its evidence is
`artifacts/windows/gpu-lease-selftest.json`. The iteration validation report
records whether the optional path actually ran.

## Primary API references

- [Task Scheduler interactive logon type](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)
- [RunEx and an explicit interactive session](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/nf-taskschd-iregisteredtask-runex)
- [Microsoft Terminal default-terminal specification, explicit inbox conhost launch](https://github.com/microsoft/terminal/blob/main/doc/specs/%23492%20-%20Default%20Terminal/spec.md)
- [Console creation](https://learn.microsoft.com/en-us/windows/console/creation-of-a-console)
- [GetConsoleWindow and the pseudoconsole visibility caveat](https://learn.microsoft.com/en-us/windows/console/getconsolewindow)
