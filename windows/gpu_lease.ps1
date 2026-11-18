# SPDX-License-Identifier: MIT
# Dot-source for Enter/Exit. A copied one-shot task uses -WatchDirectory.
[CmdletBinding()]
param([string]$WatchDirectory, [string]$RequestSha256)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:NeuroGpuModuleRoot = $PSScriptRoot

function Initialize-NeuroGpuNative {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Native Windows is required for GPU handoff.' }
    if (-not ('Neuro.Windows.GpuLeaseNative' -as [type])) {
        Add-Type -Path (Join-Path $script:NeuroGpuModuleRoot 'GpuLeaseNative.cs')
    }
}
function Get-NeuroGpuActiveOwnerSession([string]$OwnerSid) {
    return [Neuro.Windows.GpuLeaseNative]::ActiveOwnerSession($OwnerSid)
}
function Get-NeuroGpuStateRoot {
    return (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'neuro/gpu-leases')
}
function Get-NeuroGpuHash([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
        finally { $stream.Dispose() }
    } finally { $sha.Dispose() }
}
function Write-NeuroGpuJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 16) + "`n", [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $Path) }
}
function Read-NeuroGpuJson([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -or $item.Length -gt 65536) { throw 'Invalid GPU handoff record size.' }
    return ([IO.File]::ReadAllText($item.FullName) | ConvertFrom-Json)
}
function Resolve-NeuroGpuFile([string]$Path) {
    if (-not $Path -or $Path -notmatch '^[A-Za-z]:[\\/]' -or $Path -match '[\x00-\x1f"%!^&|<>]' -or
        @($Path -split '[\\/]' | Where-Object { $_ -eq '..' }).Count -gt 0) {
        throw 'GPU handoff files must use explicit safe Windows drive paths.'
    }
    $item = Get-Item -LiteralPath ([IO.Path]::GetFullPath($Path))
    if ($item.PSIsContainer) { throw 'GPU handoff expected a regular file.' }
    $part = $item
    while ($null -ne $part) {
        if (($part.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are unsupported for GPU handoff files.' }
        if ($part -is [IO.FileInfo]) { $part = $part.Directory } else { $part = $part.Parent }
    }
    return $item.FullName
}
function Read-NeuroGpuProfile([string]$RepositoryRoot) {
    $path = Join-Path $RepositoryRoot 'configs/gpu-owner.local.json'
    $profile = Read-NeuroGpuJson $path
    $fields = @('schema_version', 'owner_sid', 'miner_executable', 'miner_sha256', 'restart_batch', 'restart_batch_sha256')
    if (@(Compare-Object $fields @($profile.PSObject.Properties.Name)).Count -ne 0 -or
        $profile.schema_version -isnot [int] -or $profile.schema_version -ne 1) { throw 'Invalid GPU owner profile schema.' }
    foreach ($key in @('miner_sha256', 'restart_batch_sha256')) {
        if ($profile.$key -isnot [string] -or $profile.$key -cnotmatch '^[0-9a-f]{64}$') { throw 'GPU owner profile requires exact SHA-256 hashes.' }
    }
    if ($profile.owner_sid -isnot [string] -or $profile.owner_sid -cnotmatch '^S-1-(?:\d+-)+\d+$' -or
        $profile.owner_sid -cne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { throw 'GPU profile must name the current SSH/desktop owner SID.' }
    $profile.miner_executable = Resolve-NeuroGpuFile $profile.miner_executable
    $profile.restart_batch = Resolve-NeuroGpuFile $profile.restart_batch
    $null = [Neuro.Windows.GpuLeaseNative]::BatchCommand($profile.restart_batch)
    if ([IO.Path]::GetExtension($profile.miner_executable) -ine '.exe' -or
        [IO.Path]::GetDirectoryName($profile.miner_executable) -ine [IO.Path]::GetDirectoryName($profile.restart_batch)) {
        throw 'Configured miner executable and guided-setup.bat must share their directory.'
    }
    Assert-NeuroGpuProfileHashes $profile
    return $profile
}
function Assert-NeuroGpuProfileHashes($Profile) {
    if ((Get-NeuroGpuHash $Profile.miner_executable) -cne $Profile.miner_sha256 -or
        (Get-NeuroGpuHash $Profile.restart_batch) -cne $Profile.restart_batch_sha256) {
        throw 'Configured miner or restart batch changed; refusing to stop/restart it.'
    }
}
function Test-NeuroGpuIdentityAlive($Identity) {
    return [Neuro.Windows.GpuLeaseNative]::IsSameAlive([int]$Identity.Pid, [long]$Identity.CreatedFileTime,
        [string]$Identity.ExecutablePath, [string]$Identity.OwnerSid)
}
function Stop-NeuroGpuExact($Identity) {
    return [Neuro.Windows.GpuLeaseNative]::StopExact([int]$Identity.Pid, [long]$Identity.CreatedFileTime,
        [string]$Identity.ExecutablePath, [string]$Identity.OwnerSid)
}
function Get-NeuroGpuPathProcesses([string]$Executable, [string]$OwnerSid) {
    $name = [IO.Path]::GetFileName($Executable).Replace("'", "''")
    foreach ($row in @(Get-CimInstance Win32_Process -Filter ("Name='" + $name + "'"))) {
        $identity = [Neuro.Windows.GpuLeaseNative]::Inspect([int]$row.ProcessId)
        if ($null -eq $identity -or $identity.ExecutablePath -ine $Executable) { continue }
        if ($identity.OwnerSid -cne $OwnerSid) { throw 'Configured GPU executable is running under another owner.' }
        $identity
    }
}
function Test-NeuroGpuKnownLauncher([string]$CommandLine, [string]$Batch) {
    $cmd = [regex]::Escape((Join-Path ([Environment]::SystemDirectory) 'cmd.exe'))
    $pattern = '^\s*"?(?:' + $cmd + '|cmd(?:\.exe)?)"?\s+(?:/[dsq]\s+)*/[ck]\s+(?<payload>.+)$'
    $match = [regex]::Match($CommandLine, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) { return $false }
    return $match.Groups['payload'].Value.Trim().Trim([char[]]@('"', ' ')).Trim() -ieq $Batch
}
function Get-NeuroGpuStopPlan($Profile) {
    $miners = @(Get-NeuroGpuPathProcesses $Profile.miner_executable $Profile.owner_sid)
    if ($miners.Count -gt 1) { throw 'Multiple configured miner processes are ambiguous; no process was stopped.' }
    $launchers = @()
    foreach ($miner in $miners) {
        $row = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $miner.Pid)
        if ($null -eq $row) { throw 'Miner changed during preflight.' }
        if ([int]$row.ParentProcessId -le 0) { continue }
        $parent = [Neuro.Windows.GpuLeaseNative]::Inspect([int]$row.ParentProcessId)
        if ($null -eq $parent -or $parent.CreatedFileTime -gt $miner.CreatedFileTime) { continue }
        if ($parent.OwnerSid -cne $Profile.owner_sid) { throw 'Unknown miner launcher owner; no process was stopped.' }
        $cmdPath = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
        $explorerPath = Join-Path $env:SystemRoot 'explorer.exe'
        if ($parent.ExecutablePath -ieq $explorerPath) { continue }
        $parentRow = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $parent.Pid)
        if ($parent.ExecutablePath -ine $cmdPath -or $null -eq $parentRow -or
            -not (Test-NeuroGpuKnownLauncher ([string]$parentRow.CommandLine) $Profile.restart_batch)) {
            throw 'Unknown miner launcher; cannot rule out automatic respawn. No process was stopped.'
        }
        foreach ($child in @(Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $parent.Pid))) {
            if ([int]$child.ProcessId -eq $miner.Pid) { continue }
            $childIdentity = [Neuro.Windows.GpuLeaseNative]::Inspect([int]$child.ProcessId)
            if ($null -ne $childIdentity -and $childIdentity.ExecutablePath -ine (Join-Path ([Environment]::SystemDirectory) 'conhost.exe')) {
                throw 'Known batch owns an unexpected child; no process was stopped.'
            }
        }
        $launchers += $parent
    }
    return [pscustomobject]@{ miners = $miners; launchers = $launchers }
}
function Test-NeuroGpuRestoredLineage($Miner, $Launcher, $Request) {
    if ($Miner.CreatedFileTime -lt $Launcher.CreatedFileTime -or $Miner.SessionId -ne $Request.session_id) { return $false }
    $row = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $Miner.Pid)
    if ($null -eq $row -or [int]$row.ParentProcessId -le 0) { return $false }
    $parent = [Neuro.Windows.GpuLeaseNative]::Inspect([int]$row.ParentProcessId)
    if ($null -eq $parent -or $parent.ExecutablePath -ine (Join-Path ([Environment]::SystemDirectory) 'cmd.exe') -or
        $parent.OwnerSid -cne $Request.profile.owner_sid -or $parent.SessionId -ne $Request.session_id -or
        $parent.CreatedFileTime -lt $Launcher.CreatedFileTime -or $parent.CreatedFileTime -gt $Miner.CreatedFileTime) { return $false }
    $parentRow = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $parent.Pid)
    return $null -ne $parentRow -and [int]$parentRow.ParentProcessId -eq $Launcher.Pid -and
        (Test-NeuroGpuKnownLauncher ([string]$parentRow.CommandLine) $Request.profile.restart_batch) -and
        (Test-NeuroGpuIdentityAlive $Launcher)
}
function Get-NeuroGpuBusyProcesses($Request) {
    foreach ($path in $Request.gpu_executables) { Get-NeuroGpuPathProcesses $path $Request.profile.owner_sid }
}
function Wait-NeuroGpuReleased($Request, [int]$Seconds = 90) {
    $until = [DateTime]::UtcNow.AddSeconds($Seconds); $quiet = 0
    while ([DateTime]::UtcNow -lt $until) {
        if (@(Get-NeuroGpuBusyProcesses $Request).Count -eq 0) { $quiet++ } else { $quiet = 0 }
        if ($quiet -ge 2) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}
function Update-NeuroGpuRestorationObservation($Record, [long]$Window, [object[]]$Miners, [bool]$LineageMatches) {
    $Record.visible_hwnd = $Window; $Record.window_visible = $Window -gt 0
    # Observations belong to this poll. Never combine a previously seen miner
    # with a later window after that miner has already exited or changed lineage.
    $Record.miner_observed = $false; $Record.restored_miner = $null; $Record.passed = $false
    if ($null -ne $Miners -and $Miners.Count -eq 1 -and $LineageMatches -and (Test-NeuroGpuIdentityAlive $Miners[0])) {
        $Record.miner_observed = $true; $Record.restored_miner = $Miners[0]
    }
    $Record.passed = $Record.window_visible -and $Record.miner_observed
}
function Get-NeuroGpuRecoveryDecision($Request, [bool]$GpuReleased, [bool]$StopStarted, [bool]$RestartIssued, [object[]]$Existing) {
    if ($null -eq $Existing) { $Existing = @() }
    if (-not $GpuReleased) { throw 'Owned GPU test processes are still present; miner restart is blocked.' }
    if (-not $StopStarted) { return 'untouched' }
    if (-not $Request.miner_was_running) {
        if ($Existing.Count -ne 0) { throw 'Miner appeared although stopped at entry; its origin is unknown.' }
        return 'preserve_stopped'
    }
    if ($Existing.Count -eq 1) {
        $original = $Request.original_miners[0]
        if ([Neuro.Windows.GpuLeaseNative]::Matches($Existing[0], [int]$original.Pid, [long]$original.CreatedFileTime,
            $original.ExecutablePath, $original.OwnerSid)) { return 'preserve_original' }
        throw 'A different miner already appeared; refusing a duplicate batch launch.'
    }
    if ($Existing.Count -ne 0) { throw 'Multiple miners appeared; refusing a duplicate batch launch.' }
    if ($RestartIssued) { throw 'A restart was already issued; inspect its result instead of duplicating it.' }
    return 'restart_batch'
}
function Invoke-NeuroGpuDesktopPreflight($Request) {
    # No console and no extra process. GPU probes already use CreateNoWindow.
    # The only visible window in this lease is the restored miner batch.
    [Neuro.Windows.GpuLeaseNative]::AssertOwnerDesktop($Request.profile.owner_sid, [int]$Request.session_id)
}
function Start-NeuroGpuWatchTask($Request, [string]$Directory, [string]$Hash) {
    $service = New-Object -ComObject 'Schedule.Service'; $service.Connect()
    $folder = $service.GetFolder('\'); $task = $service.NewTask(0)
    $task.RegistrationInfo.Description = 'One bounded neuro GPU lease recovery; restores an already-running owner miner.'
    $task.Principal.UserId = $Request.profile.owner_sid
    $task.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN; no password, no S4U/System.
    $task.Principal.RunLevel = 1
    $task.Settings.Enabled = $true; $task.Settings.Hidden = $false
    $task.Settings.DisallowStartIfOnBatteries = $false; $task.Settings.StopIfGoingOnBatteries = $false
    $task.Settings.ExecutionTimeLimit = 'PT20M'; $task.Settings.MultipleInstances = 2
    $task.Settings.AllowDemandStart = $true; $task.Settings.RunOnlyIfIdle = $false
    $task.Settings.IdleSettings.StopOnIdleEnd = $false; $task.Settings.IdleSettings.RestartOnIdle = $false
    $action = $task.Actions.Create(0)
    $action.Path = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell/v1.0/powershell.exe'
    $script = Join-Path $Directory 'gpu_lease.ps1'
    $action.Arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '" -WatchDirectory "' + $Directory + '" -RequestSha256 ' + $Hash
    $action.WorkingDirectory = $Directory
    $registered = $folder.RegisterTaskDefinition($Request.task_name, $task, 2, $Request.profile.owner_sid, $null, 3, $null)
    # The scheduler service creates this process outside the SSH action's Job Object.
    $null = $registered.RunEx($null, 4, [int]$Request.session_id, $null)
}
function Register-NeuroGpuRestartTask($Request) {
    $service = New-Object -ComObject 'Schedule.Service'; $service.Connect()
    $folder = $service.GetFolder('\'); $task = $service.NewTask(0)
    $task.RegistrationInfo.Description = 'Owner miner restored after one neuro GPU lease; exact configured visible batch.'
    $task.Principal.UserId = $Request.profile.owner_sid; $task.Principal.LogonType = 3; $task.Principal.RunLevel = 1
    $task.Settings.Enabled = $true; $task.Settings.Hidden = $false
    $task.Settings.DisallowStartIfOnBatteries = $false; $task.Settings.StopIfGoingOnBatteries = $false
    $task.Settings.ExecutionTimeLimit = 'PT0S'; $task.Settings.MultipleInstances = 2
    $task.Settings.AllowDemandStart = $true; $task.Settings.RunOnlyIfIdle = $false
    $task.Settings.IdleSettings.StopOnIdleEnd = $false; $task.Settings.IdleSettings.RestartOnIdle = $false
    $action = $task.Actions.Create(0)
    $action.Path = Join-Path ([Environment]::SystemDirectory) 'conhost.exe'
    $action.Arguments = '"' + (Join-Path ([Environment]::SystemDirectory) 'cmd.exe') + '" ' + [Neuro.Windows.GpuLeaseNative]::BatchCommand($Request.profile.restart_batch)
    $action.WorkingDirectory = [IO.Path]::GetDirectoryName($Request.profile.restart_batch)
    return $folder.RegisterTaskDefinition($Request.restart_task_name, $task, 2, $Request.profile.owner_sid, $null, 3, $null)
}
function Start-NeuroGpuRestartTask($Request, $Task) {
    $started = [DateTime]::UtcNow.ToFileTimeUtc()
    $null = $Task.RunEx($null, 4, [int]$Request.session_id, $null)
    $until = [DateTime]::UtcNow.AddSeconds(15)
    $expectedArguments = '"' + (Join-Path ([Environment]::SystemDirectory) 'cmd.exe') + '" ' + [Neuro.Windows.GpuLeaseNative]::BatchCommand($Request.profile.restart_batch)
    while ([DateTime]::UtcNow -lt $until) {
        $found = @()
        foreach ($row in @(Get-CimInstance Win32_Process -Filter "Name='conhost.exe'")) {
            if ([int]$row.SessionId -ne [int]$Request.session_id) { continue }
            $identity = [Neuro.Windows.GpuLeaseNative]::Inspect([int]$row.ProcessId)
            if ($null -eq $identity -or $identity.CreatedFileTime -lt $started -or
                $identity.ExecutablePath -ine (Join-Path ([Environment]::SystemDirectory) 'conhost.exe') -or
                $identity.OwnerSid -cne $Request.profile.owner_sid -or $identity.SessionId -ne $Request.session_id) { continue }
            $command = [string]$row.CommandLine
            # Scheduler may quote its executable token. The exact action tail is
            # fixed and includes the configured batch; unrelated conhosts differ.
            if ($command.EndsWith($expectedArguments, [StringComparison]::OrdinalIgnoreCase)) { $found += $identity }
        }
        if ($found.Count -gt 1) { throw 'Ambiguous newly launched visible console; no duplicate restart will be issued.' }
        if ($found.Count -eq 1) { return $found[0] }
        Start-Sleep -Milliseconds 200
    }
    throw 'Restart task was issued but its exact console process was not observed.'
}
function Remove-NeuroGpuWatchTask([string]$Name) {
    try { $service = New-Object -ComObject 'Schedule.Service'; $service.Connect(); $service.GetFolder('\').DeleteTask($Name, 0) }
    catch { } # Result evidence persists even if task cleanup is denied.
}
function New-NeuroGpuMutex {
    param([ValidateRange(0,30000)][int]$WaitMilliseconds = 0)
    $mutex = [Threading.Mutex]::new($false, 'Global\NeuroGpuOwner-v1')
    try { $acquired = $mutex.WaitOne($WaitMilliseconds) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { $mutex.Dispose(); throw 'Another GPU lease owns this Windows host.' }
    return $mutex
}
function Enter-NeuroGpuLease {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$RepositoryRoot, [ValidateRange(0,15)][int]$Device = 0)
    Initialize-NeuroGpuNative
    $mutex = New-NeuroGpuMutex
    $lease = $null
    try {
        $root = Get-NeuroGpuStateRoot
        [IO.Directory]::CreateDirectory($root) | Out-Null
        $pointerPath = Join-Path $root 'current.json'
        if ([IO.File]::Exists($pointerPath)) {
            $old = Read-NeuroGpuJson $pointerPath
            $prior = Join-Path $root ($old.request_id + '/result.json')
            if (-not [IO.File]::Exists($prior) -or (Read-NeuroGpuJson $prior).passed -ne $true) {
                throw 'An unresolved prior GPU lease exists; inspect its durable journal before another GPU run.'
            }
        }
        $profile = Read-NeuroGpuProfile $RepositoryRoot
        $plan = Get-NeuroGpuStopPlan $profile
        $session = Get-NeuroGpuActiveOwnerSession $profile.owner_sid
        foreach ($miner in $plan.miners) {
            if ($miner.SessionId -ne $session) { throw 'Configured miner is outside the active owner session.' }
        }
        $id = [Guid]::NewGuid().ToString('N'); $directory = Join-Path $root $id
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        foreach ($name in @('gpu_lease.ps1','GpuLeaseNative.cs')) {
            $original = Join-Path $script:NeuroGpuModuleRoot $name
            $copied = Join-Path $directory $name
            [IO.File]::Copy($original, $copied, $false)
            if ((Get-NeuroGpuHash $original) -cne (Get-NeuroGpuHash $copied)) { throw 'Copied GPU recovery source differs from its candidate.' }
        }
        $paths = @('neuro_mma_probe.exe','neuro_fullk_probe.exe','neuro_pearl_probe.exe','neuro_pool_replay_client_cuda.exe') |
            ForEach-Object { [IO.Path]::GetFullPath((Join-Path $RepositoryRoot ('build/windows/' + $_))) }
        $request = [ordered]@{
            schema = 'neuro.gpu_lease.request.v1'; request_id = $id; device = $Device
            created_at = [DateTime]::UtcNow.ToString('o'); owner = [Neuro.Windows.GpuLeaseNative]::Inspect($PID)
            profile = $profile; session_id = $session; miner_was_running = ($plan.miners.Count -eq 1)
            original_miners = @($plan.miners); launchers = @($plan.launchers); gpu_executables = @($paths)
            task_name = 'NeuroGpuLease-' + $id; restart_task_name = 'NeuroGpuMiner-' + $id
            module_sha256 = Get-NeuroGpuHash (Join-Path $directory 'gpu_lease.ps1'); helper_sha256 = Get-NeuroGpuHash (Join-Path $directory 'GpuLeaseNative.cs')
        }
        if (@(Get-NeuroGpuBusyProcesses $request).Count -ne 0) { throw 'A previous GPU test executable is still running.' }
        $requestPath = Join-Path $directory 'request.json'; Write-NeuroGpuJson $requestPath $request
        $lease = [pscustomobject]@{ Mutex = $mutex; Directory = $directory; Request = $request; Closed = $false }
        Write-NeuroGpuJson $pointerPath @{ request_id = $id }
        Start-NeuroGpuWatchTask $request $directory (Get-NeuroGpuHash $requestPath)
        $until = [DateTime]::UtcNow.AddSeconds(30); $readyPath = Join-Path $directory 'ready.json'
        while (-not [IO.File]::Exists($readyPath) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 200 }
        if (-not [IO.File]::Exists($readyPath)) { throw 'Detached interactive GPU recovery watcher did not become ready.' }
        $ready = Read-NeuroGpuJson $readyPath
        $attached = $ready.PSObject.Properties.Name -contains 'desktop_attached' -and $ready.desktop_attached -eq $true
        if ($ready.ready -ne $true -or $ready.request_id -cne $id -or -not $attached) {
            $detail = $null
            if ($ready.PSObject.Properties.Name -contains 'error') { $detail = [string]$ready.error }
            if ([string]::IsNullOrWhiteSpace($detail)) { $detail = 'owner desktop was not attached' }
            throw ('Owner desktop attach preflight failed; miner remains untouched. ' + $detail)
        }
        Assert-NeuroGpuProfileHashes $profile
        Write-NeuroGpuJson (Join-Path $directory 'stop-started.json') @{ request_id = $id }
        foreach ($launcher in $plan.launchers) { $null = Stop-NeuroGpuExact $launcher }
        foreach ($miner in $plan.miners) { $null = Stop-NeuroGpuExact $miner }
        for ($i = 0; $i -lt 4; $i++) {
            if (@(Get-NeuroGpuPathProcesses $profile.miner_executable $profile.owner_sid).Count -ne 0) {
                throw 'Configured miner remained or respawned; GPU work is prohibited.'
            }
            Start-Sleep -Milliseconds 500
        }
        return $lease
    } catch {
        $failure = $_
        if ($null -ne $lease) {
            if (-not [IO.File]::Exists((Join-Path $lease.Directory 'stop-started.json'))) {
                # The owner miner has not been touched. Resolve a failed task
                # registration/desktop preflight without a 180-second wait or
                # an unnecessary permanent block on the next repaired attempt.
                try {
                    Write-NeuroGpuJson (Join-Path $lease.Directory 'release.json') @{ request_id = $lease.Request.request_id }
                    try {
                        $service = New-Object -ComObject 'Schedule.Service'; $service.Connect()
                        $service.GetFolder('\').GetTask($lease.Request.task_name).Stop(0)
                    } catch { }
                    Write-NeuroGpuJson (Join-Path $lease.Directory 'result.json') @{
                        schema = 'neuro.gpu_handoff.v1'; request_id = $lease.Request.request_id
                        passed = $true; original_preserved = $true; preflight_failed = $true
                        restart_requested = $false; batch_launched = $false; error = $failure.Exception.Message
                    }
                    Remove-NeuroGpuWatchTask $lease.Request.task_name
                    Remove-NeuroGpuWatchTask $lease.Request.restart_task_name
                } finally { $lease.Closed = $true; $mutex.ReleaseMutex(); $mutex.Dispose() }
            } else {
                try { $null = Exit-NeuroGpuLease -Lease $lease } catch { }
            }
        } else { $mutex.ReleaseMutex(); $mutex.Dispose() }
        throw $failure
    }
}
function Exit-NeuroGpuLease {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Lease)
    if ($Lease.Closed) { throw 'GPU lease already closed.' }
    try {
        Write-NeuroGpuJson (Join-Path $Lease.Directory 'release.json') @{ request_id = $Lease.Request.request_id }
        $resultPath = Join-Path $Lease.Directory 'result.json'; $until = [DateTime]::UtcNow.AddSeconds(180)
        while (-not [IO.File]::Exists($resultPath) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 200 }
        if (-not [IO.File]::Exists($resultPath)) {
            return [pscustomobject]@{ schema = 'neuro.gpu_handoff.v1'; request_id = $Lease.Request.request_id
                passed = $false; recovery_pending = $true; journal = $Lease.Directory
                error = 'Detached recovery result is unavailable; do not start another GPU run.' }
        }
        $result = Read-NeuroGpuJson $resultPath
        if ($result.request_id -cne $Lease.Request.request_id) { throw 'GPU recovery result identity mismatch.' }
        return $result
    } finally {
        $Lease.Closed = $true; $Lease.Mutex.ReleaseMutex(); $Lease.Mutex.Dispose()
    }
}
function Invoke-NeuroGpuWatch([string]$Directory, [string]$ExpectedHash) {
    if ([IO.Path]::GetFullPath($Directory).TrimEnd('\') -ine [IO.Path]::GetFullPath($script:NeuroGpuModuleRoot).TrimEnd('\')) {
        throw 'GPU watcher must execute its copied journal module.'
    }
    $requestPath = Join-Path $Directory 'request.json'
    if ($ExpectedHash -cnotmatch '^[a-f0-9]{64}$' -or (Get-NeuroGpuHash $requestPath) -cne $ExpectedHash) { throw 'GPU watcher request changed.' }
    $request = Read-NeuroGpuJson $requestPath
    if ($request.schema -cne 'neuro.gpu_lease.request.v1' -or $request.request_id -cnotmatch '^[a-f0-9]{32}$' -or
        (Get-NeuroGpuHash (Join-Path $Directory 'gpu_lease.ps1')) -cne $request.module_sha256 -or
        (Get-NeuroGpuHash (Join-Path $Directory 'GpuLeaseNative.cs')) -cne $request.helper_sha256) { throw 'GPU watcher source/identity mismatch.' }
    Initialize-NeuroGpuNative
    $result = [ordered]@{
        schema = 'neuro.gpu_handoff.v1'; request_id = $request.request_id; passed = $false
        miner_was_running = $request.miner_was_running; test_processes_exited = $false
        owner_exit_observed = $false; release_requested = $false; restart_requested = $false
        batch_launched = $false; window_visible = $false; visible_hwnd = 0L
        miner_observed = $false; restored_miner = $null; launcher = $null; original_preserved = $false
        actual_windows_execution = $true; physical_gpu_execution = $false; error = $null
    }
    $recoveryMutex = $null
    try {
        $self = [Neuro.Windows.GpuLeaseNative]::Inspect($PID)
        if ($self.OwnerSid -cne $request.profile.owner_sid -or $self.SessionId -ne $request.session_id -or
            [Neuro.Windows.GpuLeaseNative]::ActiveOwnerSession($request.profile.owner_sid) -ne $request.session_id) {
            throw 'Recovery watcher is not in the active owner desktop session.'
        }
        Assert-NeuroGpuProfileHashes $request.profile
        $restartTask = Register-NeuroGpuRestartTask $request
        $restartDefinition = [string]$restartTask.Xml
        Invoke-NeuroGpuDesktopPreflight $request
        Write-NeuroGpuJson (Join-Path $Directory 'ready.json') @{ ready = $true; request_id = $request.request_id; desktop_attached = $true }
        $until = [DateTime]::UtcNow.AddSeconds(930)
        while ([DateTime]::UtcNow -lt $until) {
            $result.owner_exit_observed = -not (Test-NeuroGpuIdentityAlive $request.owner)
            $releasePath = Join-Path $Directory 'release.json'
            if ([IO.File]::Exists($releasePath)) {
                $result.release_requested = (Read-NeuroGpuJson $releasePath).request_id -ceq $request.request_id
            }
            if ($result.owner_exit_observed -or $result.release_requested) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not $result.owner_exit_observed -and -not $result.release_requested) { throw 'GPU lease owner remained active past its bounded recovery deadline.' }
        if ($result.owner_exit_observed) {
            # An abandoned mutex may be acquired; the durable unresolved pointer
            # prevents a new caller from starting GPU work before this recovery.
            $recoveryMutex = New-NeuroGpuMutex -WaitMilliseconds 30000
        }
        $result.test_processes_exited = Wait-NeuroGpuReleased $request 90
        $existing = @(Get-NeuroGpuPathProcesses $request.profile.miner_executable $request.profile.owner_sid)
        $restartMarker = Join-Path $Directory 'restart-issued.json'
        $decision = Get-NeuroGpuRecoveryDecision $request $result.test_processes_exited `
            ([IO.File]::Exists((Join-Path $Directory 'stop-started.json'))) ([IO.File]::Exists($restartMarker)) $existing
        if ($decision -ne 'restart_batch') {
            $result.original_preserved = $true
            if ($decision -eq 'preserve_original') { $result.miner_observed = $true; $result.restored_miner = $existing[0] }
            $result.passed = $true; return
        }
        Assert-NeuroGpuProfileHashes $request.profile
        $result.restart_requested = $true
        Write-NeuroGpuJson $restartMarker @{ request_id = $request.request_id }
        if ([string]$restartTask.Xml -cne $restartDefinition) { throw 'Registered restart task changed after preflight.' }
        $result.launcher = Start-NeuroGpuRestartTask $request $restartTask
        $result.batch_launched = $true
        $until = [DateTime]::UtcNow.AddSeconds(45)
        while ([DateTime]::UtcNow -lt $until) {
            $visible = [Neuro.Windows.GpuLeaseNative]::VisibleConsoleWindow($result.launcher.Pid, $result.launcher.CreatedFileTime, $request.profile.owner_sid, [int]$request.session_id)
            $miners = @(Get-NeuroGpuPathProcesses $request.profile.miner_executable $request.profile.owner_sid)
            if ($miners.Count -gt 1) { throw 'Multiple miner processes appeared after restart.' }
            $lineage = $miners.Count -eq 1 -and (Test-NeuroGpuRestoredLineage $miners[0] $result.launcher $request)
            Update-NeuroGpuRestorationObservation $result $visible $miners $lineage
            if ($result.passed) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not $result.passed) { throw 'Batch launch was issued but a visible console and running miner were not both observed.' }
    } catch {
        $result.error = $_.Exception.Message
        if (-not [IO.File]::Exists((Join-Path $Directory 'stop-started.json'))) {
            # Preflight failure never changed the owner's miner. The GPU action
            # still fails, but its handoff is resolved and may be tried after repair.
            $result.original_preserved = $true; $result.passed = $true
        }
        if (-not [IO.File]::Exists((Join-Path $Directory 'ready.json'))) {
            Write-NeuroGpuJson (Join-Path $Directory 'ready.json') @{ ready = $false; request_id = $request.request_id; desktop_attached = $false; error = $result.error }
        }
    } finally {
        $result.finished_at = [DateTime]::UtcNow.ToString('o')
        Write-NeuroGpuJson (Join-Path $Directory 'result.json') $result
        if ($null -ne $recoveryMutex) { $recoveryMutex.ReleaseMutex(); $recoveryMutex.Dispose() }
        Remove-NeuroGpuWatchTask $request.task_name
        if (-not $result.restart_requested) { Remove-NeuroGpuWatchTask $request.restart_task_name }
    }
}
if ($WatchDirectory) { Invoke-NeuroGpuWatch ([IO.Path]::GetFullPath($WatchDirectory)) $RequestSha256 }
