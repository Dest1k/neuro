# SPDX-License-Identifier: MIT
# Real Windows process tests + an explicitly synthetic desktop/scheduler adapter.
[CmdletBinding()]
param([switch]$InteractiveSmoke)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'gpu_lease.ps1')
Initialize-NeuroGpuNative
$script:Checks = 0
$script:Cases = [Collections.Generic.List[string]]::new()
function Assert-Gpu([bool]$Condition, [string]$Name) {
    if (-not $Condition) { throw ('GPU lease check failed: ' + $Name) }
    $script:Checks++; $script:Cases.Add($Name)
}
function Assert-GpuThrows([scriptblock]$Body, [string]$Name) {
    $caught = $false
    try { $null = & $Body } catch { $caught = $true }
    Assert-Gpu $caught $Name
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('neuro-gpu-lease-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixture) | Out-Null
$fixtureRepo = Join-Path $fixture 'repo'
[IO.Directory]::CreateDirectory((Join-Path $fixtureRepo 'configs')) | Out-Null
$script:PrivateState = Join-Path $fixture 'leases'
$script:Owned = [Collections.Generic.List[object]]::new()
$script:OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$script:DesktopSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
$fixtureExe = Join-Path $fixture 'fixture-miner.exe'
$fixtureBatch = Join-Path $fixture 'guided-setup.bat'
$marker = Join-Path $fixture 'miner-started.txt'
$source = Join-Path $fixture 'fixture-miner.cs'
[IO.File]::WriteAllText($source, @'
using System; using System.Diagnostics; using System.IO; using System.Threading;
class FixtureMiner { static void Main(string[] args) {
    if (args.Length > 0) File.WriteAllText(args[0], Process.GetCurrentProcess().Id.ToString());
    Thread.Sleep(90000);
} }
'@)
$csc = Join-Path $env:SystemRoot 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
$compilerArguments = (@('/nologo', '/target:exe', ('/out:' + $fixtureExe), $source) | ForEach-Object {
    [Neuro.Windows.BoundedProcess]::QuoteArgument($_)
}) -join ' '
$compiler = New-Object Diagnostics.ProcessStartInfo
$compiler.FileName = $csc
$compiler.Arguments = $compilerArguments
$compiler.UseShellExecute = $false
$compiler.CreateNoWindow = $true
$compiler.RedirectStandardError = $true
$compile = [Diagnostics.Process]::Start($compiler)
$compilerError = $compile.StandardError.ReadToEnd()
if (-not $compile.WaitForExit(30000) -or $compile.ExitCode -ne 0) {
    throw ('Windows fixture compiler failed. ' + $compilerError)
}
[IO.File]::WriteAllText($fixtureBatch, '@echo off' + "`r`n" + '"%~dp0fixture-miner.exe" "%~dp0miner-started.txt"' + "`r`n", [Text.Encoding]::ASCII)
$ownerProfile = [ordered]@{ schema_version=1; owner_sid=$script:OwnerSid; miner_executable=$fixtureExe
    miner_sha256=(Get-NeuroGpuHash $fixtureExe); restart_batch=$fixtureBatch; restart_batch_sha256=(Get-NeuroGpuHash $fixtureBatch) }
Write-NeuroGpuJson (Join-Path $fixtureRepo 'configs/gpu-owner.local.json') $ownerProfile
function Start-FixtureMiner {
    if ([IO.File]::Exists($marker)) { [IO.File]::Delete($marker) }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
    $start.Arguments = [Neuro.Windows.GpuLeaseNative]::BatchCommand($fixtureBatch)
    $start.WorkingDirectory = $fixture; $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($start)
    $script:Owned.Add([Neuro.Windows.GpuLeaseNative]::Inspect($process.Id))
    $until = [DateTime]::UtcNow.AddSeconds(15)
    while (-not [IO.File]::Exists($marker) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 100 }
    if (-not [IO.File]::Exists($marker)) { throw 'Fixture miner did not start.' }
    $childId = [int][IO.File]::ReadAllText($marker)
    $identity = [Neuro.Windows.GpuLeaseNative]::Inspect($childId)
    if ($null -eq $identity) { throw 'Fixture miner exited before inspection.' }
    $script:Owned.Add($identity)
    return $identity
}
$originalWrite = ${function:Write-NeuroGpuJson}
$originalStartTask = ${function:Start-NeuroGpuWatchTask}
$originalOwnerSession = ${function:Get-NeuroGpuActiveOwnerSession}
$originalStateRoot = ${function:Get-NeuroGpuStateRoot}
$report = [ordered]@{ schema='neuro.gpu_lease.selftest.v1'; status='failed'; checks=0
    actual_windows_process_execution=$true; synthetic_desktop_adapter=$true
    actual_owner_connection=$false; actual_owner_desktop=$false; actual_SRBMiner_execution=$false; physical_gpu_execution=$false
    actual_task_scheduler_fixture_execution=$false; interactive_fixture='not_requested'; cases=$script:Cases; error=$null }
try {
    $profile = Read-NeuroGpuProfile $fixtureRepo
    Assert-Gpu ($profile.miner_executable -ieq $fixtureExe) 'profile hashes and exact local executable accepted'
    $copy = ($ownerProfile | ConvertTo-Json | ConvertFrom-Json); $copy.owner_sid='S-1-5-18'
    Write-NeuroGpuJson (Join-Path $fixtureRepo 'configs/gpu-owner.local.json') $copy
    Assert-GpuThrows { Read-NeuroGpuProfile $fixtureRepo } 'foreign owner rejected before process work'
    Write-NeuroGpuJson (Join-Path $fixtureRepo 'configs/gpu-owner.local.json') $ownerProfile
    $savedBatch = [IO.File]::ReadAllBytes($fixtureBatch)
    [IO.File]::AppendAllText($fixtureBatch, 'rem changed')
    Assert-GpuThrows { Read-NeuroGpuProfile $fixtureRepo } 'changed batch rejected before process work'
    [IO.File]::WriteAllBytes($fixtureBatch, $savedBatch)
    foreach ($bad in @('C:\owner\other.bat', 'C:\owner\%EXPAND%\guided-setup.bat', '\\server\share\guided-setup.bat', 'C:\owner\x&y\guided-setup.bat')) {
        Assert-GpuThrows { [Neuro.Windows.GpuLeaseNative]::BatchCommand($bad) } ('unsafe batch command rejected: ' + $bad)
    }
    $cmdPath = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
    foreach ($line in @(('"' + $cmdPath + '" /d /s /c ""' + $fixtureBatch + '""'), ('cmd.exe /c "' + $fixtureBatch + '"'), ('cmd /q /k "' + $fixtureBatch + '"'))) {
        Assert-Gpu (Test-NeuroGpuKnownLauncher $line $fixtureBatch) 'exact batch launcher command accepted'
    }
    foreach ($tail in @(' & calc.exe', ' --extra', ' & echo guided-setup.bat')) {
        Assert-Gpu (-not (Test-NeuroGpuKnownLauncher ('cmd.exe /c "' + $fixtureBatch + '"' + $tail) $fixtureBatch)) 'chained or extra launcher arguments rejected'
    }
    Assert-Gpu (@(Get-NeuroGpuStopPlan $profile).Count -eq 1) 'empty stop plan is a defined object'
    $miner = Start-FixtureMiner
    Assert-Gpu ($miner.OwnerSid -ceq $script:OwnerSid -and $miner.ExecutablePath -ieq $fixtureExe) 'actual child identity records owner and path'
    Assert-GpuThrows { [Neuro.Windows.GpuLeaseNative]::StopExact($miner.Pid, $miner.CreatedFileTime+1, $miner.ExecutablePath, $miner.OwnerSid) } 'reused PID/creation mismatch cannot stop process'
    Assert-GpuThrows { [Neuro.Windows.GpuLeaseNative]::StopExact($miner.Pid, $miner.CreatedFileTime, $miner.ExecutablePath, 'S-1-5-18') } 'wrong owner cannot stop process'
    Assert-GpuThrows { [Neuro.Windows.GpuLeaseNative]::StopExact($miner.Pid, $miner.CreatedFileTime, ($fixtureExe+'.wrong'), $miner.OwnerSid) } 'wrong executable cannot stop process'
    Assert-Gpu (Test-NeuroGpuIdentityAlive $miner) 'identity rejections leave actual miner fixture running'
    $observation=[ordered]@{visible_hwnd=0L; window_visible=$false; miner_observed=$false; restored_miner=$null; passed=$false}
    Update-NeuroGpuRestorationObservation $observation 0 @($miner) $true
    Assert-Gpu ($observation.miner_observed -and -not $observation.passed) 'miner alone does not prove visible restoration'
    Update-NeuroGpuRestorationObservation $observation 123 @() $false
    Assert-Gpu (-not $observation.miner_observed -and -not $observation.passed -and $null -eq $observation.restored_miner) 'miner exit before window appears cannot produce stale success'
    Update-NeuroGpuRestorationObservation $observation 123 @($miner) $false
    Assert-Gpu (-not $observation.passed) 'changed miner lineage cannot reuse earlier observation'
    Update-NeuroGpuRestorationObservation $observation 123 @($miner) $true
    Assert-Gpu $observation.passed 'current live miner plus current fixture window observation passes policy'
    $plan = Get-NeuroGpuStopPlan $profile
    Assert-Gpu ($plan.miners.Count -eq 1 -and $plan.launchers.Count -eq 1) 'owned immediate guided batch launcher recognized'
    $request = [pscustomobject]@{ miner_was_running=$true; original_miners=@($miner) }
    Assert-GpuThrows { Get-NeuroGpuRecoveryDecision $request $false $true $false @() } 'busy GPU prevents restoration'
    Assert-Gpu ((Get-NeuroGpuRecoveryDecision $request $true $false $false @($miner)) -eq 'untouched') 'preflight failure preserves untouched state'
    Assert-Gpu ((Get-NeuroGpuRecoveryDecision $request $true $true $false @($miner)) -eq 'preserve_original') 'unfinished stop does not launch duplicate miner'
    Assert-Gpu ((Get-NeuroGpuRecoveryDecision $request $true $true $false @()) -eq 'restart_batch') 'originally running miner requests batch after GPU release'
    Assert-GpuThrows { Get-NeuroGpuRecoveryDecision $request $true $true $true @() } 'uncertain prior restart cannot be issued twice'
    $foreign = [Neuro.Windows.GpuProcessIdentity]::new(); $foreign.Pid=$miner.Pid; $foreign.CreatedFileTime=$miner.CreatedFileTime+1
    $foreign.ExecutablePath=$miner.ExecutablePath; $foreign.OwnerSid=$miner.OwnerSid
    Assert-GpuThrows { Get-NeuroGpuRecoveryDecision $request $true $true $false @($foreign) } 'different miner appearance blocks duplicate restoration'
    $offRequest = [pscustomobject]@{ miner_was_running=$false; original_miners=@() }
    Assert-Gpu ((Get-NeuroGpuRecoveryDecision $offRequest $true $true $false @()) -eq 'preserve_stopped') 'stopped at entry remains stopped'
    Assert-GpuThrows { Get-NeuroGpuRecoveryDecision $offRequest $true $true $false @($miner) } 'unexpected miner during stopped-at-entry lease is not accepted'

    # A rejected concurrent retry may own the mutex briefly after action death.
    # Recovery waits for it rather than abandoning restoration permanently.
    $mutexMarker=Join-Path $fixture 'mutex-held.txt'
    $childCode='$lock=[Threading.Mutex]::new($false,"Global\NeuroGpuOwner-v1");$null=$lock.WaitOne();[IO.File]::WriteAllText("' + $mutexMarker + '","ready");Start-Sleep -Milliseconds 1000;$lock.ReleaseMutex();$lock.Dispose()'
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCode))
    $start=[Diagnostics.ProcessStartInfo]::new((Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell/v1.0/powershell.exe'))
    $start.Arguments='-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true
    $other=[Diagnostics.Process]::Start($start); $script:Owned.Add([Neuro.Windows.GpuLeaseNative]::Inspect($other.Id))
    $until=[DateTime]::UtcNow.AddSeconds(15)
    while (-not [IO.File]::Exists($mutexMarker) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 50 }
    Assert-Gpu ([IO.File]::Exists($mutexMarker)) 'concurrent retry fixture holds actual cross-process mutex'
    Assert-GpuThrows { New-NeuroGpuMutex } 'normal GPU admission does not wait behind existing owner'
    $recovery=New-NeuroGpuMutex -WaitMilliseconds 5000
    Assert-Gpu ($null -ne $recovery) 'recovery waits through transient competing admission'
    $recovery.ReleaseMutex(); $recovery.Dispose()
    # Exercise production Enter/Exit and actual process stops, substituting only
    # desktop/scheduler access and its recovery callback in this fixture scope.
    function Get-NeuroGpuStateRoot { return $script:PrivateState }
    function Get-NeuroGpuActiveOwnerSession([string]$OwnerSid) { return $script:DesktopSession }
    $script:FakeRestartCount = 0; $script:FakeRequest = $null; $script:FakeDirectory = $null
    function Start-NeuroGpuWatchTask($Request, [string]$Directory, [string]$Hash) {
        Assert-Gpu ((Get-NeuroGpuHash (Join-Path $Directory 'request.json')) -ceq $Hash) 'watcher request hash binds real journal'
        $script:FakeRequest=$Request; $script:FakeDirectory=$Directory
        & $originalWrite (Join-Path $Directory 'ready.json') @{ ready=$true; request_id=$Request.request_id; visible_probe_hwnd=1 }
    }
    function Write-NeuroGpuJson([string]$Path, $Value) {
        & $originalWrite $Path $Value
        if ([IO.Path]::GetFileName($Path) -ne 'release.json') { return }
        $r=$script:FakeRequest
        $existing=@(Get-NeuroGpuPathProcesses $r.profile.miner_executable $r.profile.owner_sid)
        $decision=Get-NeuroGpuRecoveryDecision $r (@(Get-NeuroGpuBusyProcesses $r).Count -eq 0) $true $false $existing
        if ($decision -eq 'restart_batch') { $null=Start-FixtureMiner; $script:FakeRestartCount++ }
        & $originalWrite (Join-Path $script:FakeDirectory 'result.json') @{ schema='neuro.gpu_handoff.v1'; request_id=$r.request_id
            passed=$true; fixture_only=$true; synthetic_desktop_adapter=$true; actual_visible_window_observation=$false }
    }
    $lease=Enter-NeuroGpuLease -RepositoryRoot $fixtureRepo
    Assert-Gpu (@(Get-NeuroGpuPathProcesses $fixtureExe $script:OwnerSid).Count -eq 0) 'Enter really stopped only configured miner before GPU body'
    Assert-Gpu (-not (Test-NeuroGpuIdentityAlive $miner)) 'original miner process actually terminated'
    Assert-GpuThrows { Enter-NeuroGpuLease -RepositoryRoot $fixtureRepo } 'unresolved active journal rejects overlapping lease'
    $handoff=Exit-NeuroGpuLease -Lease $lease
    Assert-Gpu ($handoff.passed -and $script:FakeRestartCount -eq 1) 'normal release invokes one recovery callback'
    Assert-Gpu (@(Get-NeuroGpuPathProcesses $fixtureExe $script:OwnerSid).Count -eq 1) 'fixture batch restored real miner fixture process'
    $lease=Enter-NeuroGpuLease -RepositoryRoot $fixtureRepo
    $caught=$false
    try { throw 'synthetic GPU body failure' } catch { $caught=$true } finally { $handoff=Exit-NeuroGpuLease -Lease $lease }
    Assert-Gpu ($caught -and $handoff.passed -and $script:FakeRestartCount -eq 2) 'GPU body failure still restores through finally'
    $plan=Get-NeuroGpuStopPlan $profile
    foreach ($launcher in $plan.launchers) { $null=Stop-NeuroGpuExact $launcher }
    foreach ($value in $plan.miners) { $null=Stop-NeuroGpuExact $value }
    $lease=Enter-NeuroGpuLease -RepositoryRoot $fixtureRepo
    $handoff=Exit-NeuroGpuLease -Lease $lease
    Assert-Gpu ($handoff.passed -and $script:FakeRestartCount -eq 2) 'no restart callback when miner stopped at entry'
    Assert-Gpu (@(Get-NeuroGpuPathProcesses $fixtureExe $script:OwnerSid).Count -eq 0) 'stopped-at-entry remains actually absent'

    # Direct launch has an unknown PowerShell parent; no parent/name kill allowed.
    $start=[Diagnostics.ProcessStartInfo]::new($fixtureExe); $start.UseShellExecute=$false; $start.CreateNoWindow=$true
    $direct=[Diagnostics.Process]::Start($start); $directIdentity=[Neuro.Windows.GpuLeaseNative]::Inspect($direct.Id); $script:Owned.Add($directIdentity)
    Assert-GpuThrows { Get-NeuroGpuStopPlan $profile } 'unknown launcher blocks before stopping'
    Assert-Gpu (Test-NeuroGpuIdentityAlive $directIdentity) 'unknown launcher rejection leaves process intact'
    $null=Stop-NeuroGpuExact $directIdentity
    ${function:Write-NeuroGpuJson}=$originalWrite
    ${function:Start-NeuroGpuWatchTask}=$originalStartTask
    ${function:Get-NeuroGpuActiveOwnerSession}=$originalOwnerSession
    # Keep the test journal isolated even if the optional real scheduler smoke runs.
    if ($InteractiveSmoke) {
        $session=$null
        try { $session=Get-NeuroGpuActiveOwnerSession $script:OwnerSid } catch { $report.interactive_fixture='skipped_no_unique_active_owner_session' }
        if ($null -ne $session) {
            $null=Start-FixtureMiner
            $lease=Enter-NeuroGpuLease -RepositoryRoot $fixtureRepo
            try { Assert-Gpu (@(Get-NeuroGpuPathProcesses $fixtureExe $script:OwnerSid).Count -eq 0) 'real scheduler smoke paused fixture miner' }
            finally { $handoff=Exit-NeuroGpuLease -Lease $lease }
            Assert-Gpu ($handoff.passed -and $handoff.window_visible -and $handoff.miner_observed) 'real interactive scheduler smoke restores visible fixture batch'
            $report.actual_task_scheduler_fixture_execution=$true; $report.interactive_fixture='passed'
        }
    }
    $report.status='passed'
} catch { $report.error=$_.Exception.Message; throw }
finally {
    ${function:Write-NeuroGpuJson}=$originalWrite; ${function:Start-NeuroGpuWatchTask}=$originalStartTask
    ${function:Get-NeuroGpuActiveOwnerSession}=$originalOwnerSession; ${function:Get-NeuroGpuStateRoot}=$originalStateRoot
    foreach ($identity in $script:Owned) { if ($null -ne $identity) { try { $null=Stop-NeuroGpuExact $identity } catch { } } }
    try {
        $plan=Get-NeuroGpuStopPlan $ownerProfile
        foreach ($launcher in $plan.launchers) { $null=Stop-NeuroGpuExact $launcher }
        foreach ($value in $plan.miners) { $null=Stop-NeuroGpuExact $value }
    } catch { }
    try {
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        $folder = $service.GetFolder('\')
        foreach ($task in @($folder.GetTasks(1))) {
            if ($task.Name -notlike 'NeuroGpu*') { continue }
            if (([string]$task.Xml).IndexOf($fixture, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $folder.DeleteTask($task.Name, 0)
            }
        }
    } catch { }
    $report.checks=$script:Checks
    $evidence=Join-Path $repo 'artifacts/windows'; [IO.Directory]::CreateDirectory($evidence) | Out-Null
    & $originalWrite (Join-Path $evidence 'gpu-lease-selftest.json') $report
    Write-Output ($report | ConvertTo-Json -Depth 8 -Compress)
    try { Remove-Item -LiteralPath $fixture -Recurse -Force } catch { }
}
