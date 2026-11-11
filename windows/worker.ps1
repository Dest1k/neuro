# SPDX-License-Identifier: MIT
# Fixed offline worker. Its child action and descendants belong to one Job Object.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RequestPath,
    [string]$ReportPath,
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# CreateProcess does not apply PowerShell 7's special WinPSModulePath rewrite.
# A native Windows PowerShell child must not autoload incompatible PS7 modules.
# This changes only this worker's environment and its descendants.
if ($PSVersionTable.PSVersion.Major -le 5) {
    $workerModulePaths = @(
        [IO.Path]::Combine($PSHOME, 'Modules'),
        [IO.Path]::Combine([Environment]::GetFolderPath('ProgramFiles'), 'WindowsPowerShell\Modules')
    )
    $workerDocuments = [Environment]::GetFolderPath('MyDocuments')
    if ($workerDocuments) {
        $workerModulePaths += [IO.Path]::Combine($workerDocuments, 'WindowsPowerShell\Modules')
    }
    $env:PSModulePath = $workerModulePaths -join [IO.Path]::PathSeparator
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Get-SourceHash([string]$Path) {
    # Identity checks do not depend on module discovery or Get-FileHash aliases.
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally { $stream.Dispose() }
    } finally { $algorithm.Dispose() }
}

function Assert-Integer($Value, [long]$Minimum, [long]$Maximum, [string]$Name) {
    if (($Value -isnot [int] -and $Value -isnot [long]) -or
        $Value -lt $Minimum -or $Value -gt $Maximum) {
        throw "Invalid integer field: $Name"
    }
}

function Read-Request {
    $file = Get-Item -LiteralPath $RequestPath
    if ($file.PSIsContainer -or $file.Length -gt 8192) { throw 'Invalid request size' }
    $request = [IO.File]::ReadAllText($file.FullName) | ConvertFrom-Json
    $fields = @('schema_version', 'request_id', 'action', 'repo_root', 'device',
                'timeout_s', 'max_output_bytes', 'worker_sha256', 'runner_sha256', 'entrypoint_sha256')
    $actual = @($request.PSObject.Properties.Name)
    if (@(Compare-Object $fields $actual).Count -ne 0) { throw 'Unexpected request fields' }
    Assert-Integer $request.schema_version 1 1 'schema_version'
    if ($request.request_id -isnot [string] -or $request.request_id -cnotmatch '^[0-9a-f]{32}$') {
        throw 'Invalid request identity'
    }
    if ($request.action -cnotin @('inventory', 'build-probes', 'run-cpu', 'run-cuda')) {
        throw 'Unsupported action'
    }
    if ($request.repo_root -isnot [string] -or $request.repo_root.Length -gt 240 -or
        $request.repo_root -notmatch '^[A-Za-z]:[\\/]' -or
        $request.repo_root -match '[\x00-\x1f]' -or
        @($request.repo_root -split '[\\/]' | Where-Object { $_ -eq '..' }).Count -gt 0) {
        throw 'repo_root must be an explicit Windows drive path'
    }
    Assert-Integer $request.device 0 15 'device'
    if ($request.action -cne 'run-cuda' -and $request.device -ne 0) {
        throw 'Device is only meaningful for run-cuda'
    }
    Assert-Integer $request.timeout_s 1 3600 'timeout_s'
    Assert-Integer $request.max_output_bytes 1024 262144 'max_output_bytes'
    foreach ($field in @('worker_sha256', 'runner_sha256', 'entrypoint_sha256')) {
        if ($request.$field -isnot [string] -or $request.$field -cnotmatch '^[0-9a-f]{64}$') {
            throw "Invalid hash field: $field"
        }
    }
    $workerHash = Get-SourceHash $PSCommandPath
    $runnerHash = Get-SourceHash (Join-Path $PSScriptRoot 'BoundedProcess.cs')
    if ($workerHash -cne $request.worker_sha256 -or $runnerHash -cne $request.runner_sha256) {
        throw 'Staged worker code does not match request hashes'
    }
    return $request
}

function Get-EntrypointHash($Request) {
    $entry = Join-Path $Request.repo_root 'windows/ci.ps1'
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { return $null }
    return (Get-SourceHash $entry)
}

function Get-RepoRunnerHash($Request) {
    $runner = Join-Path $Request.repo_root 'windows/BoundedProcess.cs'
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { return $null }
    return (Get-SourceHash $runner)
}

function Inventory($Request) {
    Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
    $tools = [ordered]@{}
    foreach ($name in @('git', 'python', 'cmake', 'ninja', 'rustup', 'cargo', 'rustc', 'nvcc', 'nvidia-smi')) {
        $command = Get-Command -Name "$name.exe" -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $command) { $tools[$name] = @{ found = $false }; continue }
        $arguments = @('--version')
        if ($name -eq 'nvidia-smi') {
            $arguments = @('--query-gpu=name,driver_version', '--format=csv,noheader')
        }
        $probe = [Neuro.Windows.BoundedProcess]::Run($command.Source, $arguments,
            $PSScriptRoot, 10000, 8192)
        $tools[$name] = @{
            found = $true; exit_code = $probe.ExitCode; timed_out = $probe.TimedOut
            output_complete = $probe.OutputComplete; stdout = $probe.Stdout; stderr = $probe.Stderr
            truncated = ($probe.StdoutTruncated -or $probe.StderrTruncated); error = $probe.Error
        }
    }
    $revision = $null
    $git = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $git -and (Test-Path -LiteralPath $Request.repo_root -PathType Container)) {
        $gitResult = [Neuro.Windows.BoundedProcess]::Run($git.Source,
            @('-C', $Request.repo_root, 'rev-parse', '--verify', 'HEAD'), $PSScriptRoot, 10000, 1024)
        if ($gitResult.ExitCode -eq 0 -and $gitResult.OutputComplete -and
            -not $gitResult.TimedOut -and $gitResult.Stdout.Trim() -match '^[0-9a-f]{40}$') {
            $revision = $gitResult.Stdout.Trim()
        }
    }
    [ordered]@{
        schema = 'neuro.windows.inventory.v1'
        windows_version = [Environment]::OSVersion.Version.ToString()
        process_64_bit = [Environment]::Is64BitProcess
        powershell_version = $PSVersionTable.PSVersion.ToString()
        repo_present = (Test-Path -LiteralPath $Request.repo_root -PathType Container)
        repo_commit = $revision
        tools = $tools
    } | ConvertTo-Json -Depth 8 -Compress | Write-Output
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows is required' }
    $request = Read-Request
    $entrypointHash = Get-EntrypointHash $request
    $prerequisiteError = ''
    if ($request.action -cne 'inventory') {
        if ($entrypointHash -cne $request.entrypoint_sha256) {
            $prerequisiteError = 'Fixed action entry point is missing or differs from the requested source'
        } elseif ((Get-RepoRunnerHash $request) -cne $request.runner_sha256) {
            $prerequisiteError = 'Repository runner is missing or differs from the requested source'
        }
    }
    if ($Execute) {
        if ($request.action -ceq 'inventory') { Inventory $request; exit 0 }
        if ($prerequisiteError) { throw $prerequisiteError }
        if (-not (Test-Path -LiteralPath $request.repo_root -PathType Container)) {
            throw 'Prepositioned repository is missing'
        }
        Set-Location -LiteralPath $request.repo_root
        $entry = Join-Path $request.repo_root 'windows/ci.ps1'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'Windows action entry point is missing' }
        $global:LASTEXITCODE = 0
        if ($request.action -ceq 'run-cuda') {
            & $entry -Action $request.action -Device ([int]$request.device)
            $entrySucceeded = $?
        } else {
            & $entry -Action $request.action
            $entrySucceeded = $?
        }
        if (-not $entrySucceeded -or $LASTEXITCODE -ne 0) { exit 1 }
        exit 0
    }
    if ([string]::IsNullOrEmpty($ReportPath)) { throw 'ReportPath is required' }
    $reportFile = [IO.Path]::GetFullPath($ReportPath)
    if (Test-Path -LiteralPath $reportFile) { throw 'Refusing to overwrite an existing worker report' }
    if ($prerequisiteError) {
        # Produce a bound rejection report before launching any action process.
        $result = [pscustomobject]@{
            ExitCode = -1; TimedOut = $false; OutputComplete = $false; DurationMs = 0; ProcessId = 0
            StdoutBytes = 0; StderrBytes = 0; StdoutTruncated = $false; StderrTruncated = $false
            Stdout = ''; Stderr = ''; Error = $prerequisiteError
        }
    } else {
        Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
        $powershell = Join-Path $PSHOME 'powershell.exe'
        if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) { $powershell = Join-Path $PSHOME 'pwsh.exe' }
        $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                       '-File', $PSCommandPath, '-RequestPath', [IO.Path]::GetFullPath($RequestPath), '-Execute')
        $result = [Neuro.Windows.BoundedProcess]::Run($powershell, $arguments, $PSScriptRoot,
            ([int]$request.timeout_s * 1000), [int]$request.max_output_bytes)
    }
    $status = 'failed'; $workerExit = 1
    if ($result.Error.Length -gt 0 -or -not $result.OutputComplete) {
        $status = 'worker_error'; $workerExit = 2
    } elseif ($result.TimedOut) {
        $status = 'timeout'; $workerExit = 124
    } elseif ($result.StdoutTruncated -or $result.StderrTruncated) {
        $status = 'output_limit'; $workerExit = 3
    } elseif ($result.ExitCode -eq 0) {
        $status = 'passed'; $workerExit = 0
    }
    $report = [ordered]@{
        schema = 'neuro.remote.worker.v1'
        request_id = $request.request_id
        action = $request.action
        worker_sha256 = $request.worker_sha256
        runner_sha256 = $request.runner_sha256
        entrypoint_sha256 = $request.entrypoint_sha256
        entrypoint_actual_sha256 = $entrypointHash
        status = $status
        exit_code = $result.ExitCode
        timed_out = $result.TimedOut
        output_complete = $result.OutputComplete
        duration_ms = $result.DurationMs
        process_id = $result.ProcessId
        stdout_bytes = $result.StdoutBytes
        stderr_bytes = $result.StderrBytes
        stdout_truncated = $result.StdoutTruncated
        stderr_truncated = $result.StderrTruncated
        stdout = $result.Stdout
        stderr = $result.Stderr
        error = $result.Error
    }
    $json = $report | ConvertTo-Json -Depth 8 -Compress
    [IO.File]::WriteAllText($reportFile, $json, (New-Object Text.UTF8Encoding($false)))
    exit $workerExit
} catch {
    # Invalid requests and pre-launch errors cannot produce a successful report.
    [Console]::Error.WriteLine('Windows worker failed: ' + $_.Exception.Message)
    exit 2
}
