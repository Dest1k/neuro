# SPDX-License-Identifier: MIT
# Native Windows integration fixtures for worker.ps1, not mining evidence.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not ('Neuro.Windows.BoundedProcess' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
}
$shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$runner = Join-Path $PSScriptRoot 'BoundedProcess.cs'
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('neuro remote worker ' + [guid]::NewGuid().ToString('N'))
$staged = Join-Path $temporary 'staged-worker'
[IO.Directory]::CreateDirectory($staged) | Out-Null
$worker = Join-Path $staged 'worker.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'worker.ps1') -Destination $worker
Copy-Item -LiteralPath $runner -Destination (Join-Path $staged 'BoundedProcess.cs')
$manifestTool = Join-Path $staged 'candidate_manifest.py'
Copy-Item -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) 'tools/candidate_manifest.py') -Destination $manifestTool
$fakeRepo = Join-Path $temporary "repo with spaces ' and `$literal"
$fakeWindows = Join-Path $fakeRepo 'windows'
[IO.Directory]::CreateDirectory($fakeWindows) | Out-Null
$evidence = Join-Path (Split-Path $PSScriptRoot -Parent) 'artifacts/windows'
[IO.Directory]::CreateDirectory($evidence) | Out-Null
$fixtureRunner = Join-Path $fakeWindows 'BoundedProcess.cs'
Copy-Item -LiteralPath $runner -Destination $fixtureRunner
$fakeUpstream = Join-Path $fakeRepo 'upstream'
[IO.Directory]::CreateDirectory($fakeUpstream) | Out-Null
[IO.File]::WriteAllText((Join-Path $fakeUpstream 'sources.lock.json'), '{"schema_version":1,"sources":[]}',
    (New-Object Text.UTF8Encoding($false)))
$fakeNative = Join-Path $fakeRepo 'native'
[IO.Directory]::CreateDirectory($fakeNative) | Out-Null
$fakeKernel = Join-Path $fakeNative 'kernel.cu'
[IO.File]::WriteAllText($fakeKernel, '// fixture candidate kernel', (New-Object Text.UTF8Encoding($false)))
$git = (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$python = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$initialized = [Neuro.Windows.BoundedProcess]::Run($git, @('init', '-q', $fakeRepo), $temporary, 10000, 4096)
if ($initialized.ExitCode -ne 0 -or $initialized.Error -or $initialized.TimedOut) { throw 'Fixture Git initialization failed' }
$checks = 0
$cases = [Collections.Generic.List[object]]::new()

function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Remote worker integration failed: $Message" }
    $script:checks++
}
function Write-Fixture([string]$Body) {
    $preamble = @'
param([string]$Action, [int]$Device = 0)
$ErrorActionPreference = 'Stop'
if ($Action -ne 'run-cpu') { throw 'Unexpected action in test fixture' }
if ($PSBoundParameters.ContainsKey('Device')) { throw 'Non-CUDA action must not receive Device' }
'@
    [IO.File]::WriteAllText((Join-Path $fakeWindows 'ci.ps1'), $preamble + "`n" + $Body,
        (New-Object Text.UTF8Encoding($false)))
}
function Invoke-Worker([string]$Action, [int]$Timeout = 10, [int]$Cap = 4096,
                       [switch]$BadHash, [switch]$BadEntrypoint, [switch]$BadVerifier,
                       [switch]$ChangedSource, [switch]$ExtraSource) {
    $id = [guid]::NewGuid().ToString('N')
    $requestFile = Join-Path $temporary ($id + '.request.json')
    $reportFile = Join-Path $temporary ($id + '.report.json')
    $fixtureEntry = Join-Path $fakeWindows 'ci.ps1'
    $expectedEntry = if (Test-Path -LiteralPath $fixtureEntry -PathType Leaf) { $fixtureEntry }
                     else { Join-Path $PSScriptRoot 'ci.ps1' }
    $request = [ordered]@{
        schema_version = 2; request_id = $id; action = $Action; repo_root = $fakeRepo
        device = 0; timeout_s = $Timeout; max_output_bytes = $Cap
        worker_sha256 = (Get-FileHash -LiteralPath $worker -Algorithm SHA256).Hash.ToLowerInvariant()
        runner_sha256 = (Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash.ToLowerInvariant()
        # This is deliberately the synthetic fixture's hash for run-cpu cases.
        entrypoint_sha256 = (Get-FileHash -LiteralPath $expectedEntry -Algorithm SHA256).Hash.ToLowerInvariant()
        manifest_tool_sha256 = (Get-FileHash -LiteralPath $manifestTool -Algorithm SHA256).Hash.ToLowerInvariant()
        manifest_sha256 = $null; candidate_sha256 = $null
    }
    if ($Action -cne 'inventory') {
        $manifest = Join-Path $staged 'candidate.json'
        $created = [Neuro.Windows.BoundedProcess]::Run($python,
            @('-I', $manifestTool, 'create', '--root', $fakeRepo, '--output', $manifest), $temporary, 30000, 8192)
        Check ($created.ExitCode -eq 0 -and -not $created.TimedOut -and $created.OutputComplete) 'real Python candidate manifest created'
        $identity = $created.Stdout | ConvertFrom-Json
        $request.candidate_sha256 = $identity.candidate_sha256
        $request.manifest_sha256 = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ChangedSource) { [IO.File]::AppendAllText($fakeKernel, '// stale source') }
        if ($ExtraSource) { [IO.File]::WriteAllText((Join-Path $fakeNative 'extra.cu'), '// extra source') }
    }
    if ($BadHash) { $request.worker_sha256 = '0' * 64 }
    if ($BadVerifier) { $request.manifest_tool_sha256 = '0' * 64 }
    if ($BadEntrypoint) { $request.entrypoint_sha256 = '0' * 64 }
    [IO.File]::WriteAllText($requestFile, ($request | ConvertTo-Json -Compress),
        (New-Object Text.UTF8Encoding($false)))
    $process = [Neuro.Windows.BoundedProcess]::Run($shell,
        @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
          '-File', $worker, '-RequestPath', $requestFile, '-ReportPath', $reportFile),
        $temporary, (($Timeout + 100) * 1000), 1048576)
    $attempt = [ordered]@{
        schema = 'neuro.windows.remote_worker.attempt.v1'; request_id = $id; action = $Action
        process = $process; miner_validation = $false
    }
    [IO.File]::WriteAllText((Join-Path $evidence ('worker-attempt-' + $id + '.json')),
        ($attempt | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
    if (-not (Test-Path -LiteralPath $reportFile) -and -not $BadHash -and -not $BadVerifier) {
        Write-Host ('[worker fixture] missing report; outer exit=' + $process.ExitCode + '; error=' + $process.Error)
        if ($process.Stderr) { Write-Host $process.Stderr.Substring(0, [Math]::Min(8192, $process.Stderr.Length)) }
        if ($process.Stdout) { Write-Host $process.Stdout.Substring(0, [Math]::Min(8192, $process.Stdout.Length)) }
    }
    Check ($process.Error -eq '' -and -not $process.TimedOut -and $process.OutputComplete) 'outer worker process completed within bound'
    if ($BadHash -or $BadVerifier) {
        Check ($process.ExitCode -eq 2 -and -not (Test-Path -LiteralPath $reportFile)) 'changed worker code is rejected before execution'
        return $null
    }
    Check (Test-Path -LiteralPath $reportFile -PathType Leaf) 'worker produced a concrete report'
    $report = [IO.File]::ReadAllText($reportFile) | ConvertFrom-Json
    Check ($report.schema -ceq 'neuro.remote.worker.v2' -and $report.request_id -ceq $id -and
           $report.action -ceq $Action) 'report belongs to this exact request'
    Check ($report.worker_sha256 -ceq $request.worker_sha256 -and
           $report.runner_sha256 -ceq $request.runner_sha256 -and
           $report.entrypoint_sha256 -ceq $request.entrypoint_sha256) 'report matches requested worker and entrypoint code'
    Check ($report.manifest_tool_sha256 -ceq $request.manifest_tool_sha256 -and
           $report.manifest_sha256 -ceq $request.manifest_sha256 -and
           $report.candidate_sha256 -ceq $request.candidate_sha256) 'report binds the complete requested candidate identity'
    if ($Action -cne 'inventory' -and $report.status -ceq 'passed') {
        Check ($report.candidate_before_sha256 -ceq $request.candidate_sha256 -and
               $report.candidate_after_sha256 -ceq $request.candidate_sha256) 'successful candidate matches before and after the action'
    }
    Check ($report.stdout.Length -le $Cap -and $report.stderr.Length -le $Cap) 'worker capture is bounded'
    $cases.Add([ordered]@{ action = $Action; status = $report.status; process_exit = $process.ExitCode
                          child_exit = $report.exit_code; timed_out = $report.timed_out })
    return [pscustomobject]@{ Process = $process; Report = $report }
}

try {
    $inventory = Invoke-Worker 'inventory' 90 262144
    Check ($inventory.Process.ExitCode -eq 0 -and $inventory.Report.status -ceq 'passed') 'actual inventory succeeded'
    $payload = $inventory.Report.stdout | ConvertFrom-Json
    Check ($payload.schema -ceq 'neuro.windows.inventory.v1' -and $payload.process_64_bit -eq $true) 'inventory describes native Windows'
    Check ($null -ne $payload.tools.python -and $null -ne $payload.tools.git) 'inventory probes actual installed tools'
    Check ($null -eq $inventory.Report.entrypoint_actual_sha256) 'inventory reports missing entrypoint without executing it'

    Write-Fixture @'
$probeHash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'BoundedProcess.cs') -Algorithm SHA256).Hash
if ($probeHash.Length -ne 64) { throw 'Windows PowerShell module discovery failed' }
[Console]::Write('fixture success')
exit 0
'@
    $success = Invoke-Worker 'run-cpu'
    Check ($success.Process.ExitCode -eq 0 -and $success.Report.status -ceq 'passed' -and
           $success.Report.exit_code -eq 0 -and $success.Report.stdout -ceq 'fixture success') 'fixed-action success propagates'

    $candidateMarker = Join-Path $temporary 'stale-candidate-executed.txt'
    $escapedCandidateMarker = $candidateMarker.Replace("'", "''")
    Write-Fixture "[IO.File]::WriteAllText('$escapedCandidateMarker','executed');exit 0"
    $mismatch = Invoke-Worker 'run-cpu' 10 4096 -ChangedSource
    Check ($mismatch.Process.ExitCode -eq 2 -and $mismatch.Report.status -ceq 'worker_error' -and
           $mismatch.Report.process_id -eq 0) 'changed kernel is rejected before action launch'
    Check (-not (Test-Path -LiteralPath $candidateMarker)) 'stale kernel candidate has no action side effects'
    $mismatch = Invoke-Worker 'run-cpu' 10 4096 -ExtraSource
    Check ($mismatch.Process.ExitCode -eq 2 -and $mismatch.Report.process_id -eq 0) 'additional candidate source is rejected'
    Check (-not (Test-Path -LiteralPath $candidateMarker)) 'additional source candidate has no action side effects'

    $escapedKernel = $fakeKernel.Replace("'", "''")
    Write-Fixture "[IO.File]::AppendAllText('$escapedKernel','// changed during action');exit 0"
    $changed = Invoke-Worker 'run-cpu'
    Check ($changed.Process.ExitCode -eq 2 -and $changed.Report.status -ceq 'worker_error' -and
           $changed.Report.exit_code -eq 0 -and $changed.Report.process_id -gt 0) 'source mutation invalidates an otherwise successful action'
    Check ($changed.Report.candidate_before_sha256 -ceq $changed.Report.candidate_sha256 -and
           $changed.Report.candidate_after_sha256 -cne $changed.Report.candidate_sha256) 'pre/post identities explain source mutation'

    Write-Fixture "[IO.File]::AppendAllText('$escapedKernel','// changed during failed action');exit 7"
    $changed = Invoke-Worker 'run-cpu'
    Check ($changed.Process.ExitCode -eq 2 -and $changed.Report.status -ceq 'worker_error' -and
           $changed.Report.exit_code -ne 0) 'final source verification runs after action failure too'

    Write-Fixture "[Console]::Write('fixture failure');exit 7"
    $failure = Invoke-Worker 'run-cpu'
    Check ($failure.Process.ExitCode -eq 1 -and $failure.Report.status -ceq 'failed' -and
           $failure.Report.exit_code -ne 0) 'nested script nonzero exit cannot become success'

    Write-Fixture "throw 'fixture terminating error'"
    $failure = Invoke-Worker 'run-cpu'
    Check ($failure.Process.ExitCode -eq 1 -and $failure.Report.status -ceq 'failed' -and
           $failure.Report.exit_code -ne 0) 'nested terminating error cannot become success'

    Write-Fixture 'Start-Sleep -Seconds 30; exit 0'
    $timeout = Invoke-Worker 'run-cpu' 3
    Check ($timeout.Process.ExitCode -eq 124 -and $timeout.Report.status -ceq 'timeout' -and
           $timeout.Report.timed_out -eq $true -and $timeout.Report.exit_code -ne 0) 'remote worker enforces its own deadline'

    Write-Fixture "[Console]::Write(('x' * 32768));exit 0"
    $truncated = Invoke-Worker 'run-cpu' 10 1024
    Check ($truncated.Process.ExitCode -eq 3 -and $truncated.Report.status -ceq 'output_limit' -and
           $truncated.Report.stdout_truncated -eq $true -and $truncated.Report.stdout_bytes -eq 32768) 'truncated action is not a successful gate'

    $marker = Join-Path $temporary 'mismatched-entry-was-run.txt'
    $escapedMarker = $marker.Replace("'", "''")
    Write-Fixture "[IO.File]::WriteAllText('$escapedMarker','executed');exit 0"
    $mismatch = Invoke-Worker 'run-cpu' 10 4096 -BadEntrypoint
    Check ($mismatch.Process.ExitCode -eq 2 -and $mismatch.Report.status -ceq 'worker_error' -and
           $mismatch.Report.process_id -eq 0) 'different entrypoint is rejected before child launch'
    Check (-not (Test-Path -LiteralPath $marker)) 'mismatched entrypoint had no side effects'

    [IO.File]::WriteAllText($fixtureRunner, '// Deliberately different synthetic runner fixture.')
    $mismatch = Invoke-Worker 'run-cpu'
    Check ($mismatch.Process.ExitCode -eq 2 -and $mismatch.Report.status -ceq 'worker_error' -and
           $mismatch.Report.process_id -eq 0 -and $mismatch.Report.error -match 'Repository runner') 'different repository runner is rejected before child launch'
    Check (-not (Test-Path -LiteralPath $marker)) 'mismatched repository runner had no side effects'
    Copy-Item -LiteralPath $runner -Destination $fixtureRunner -Force

    $null = Invoke-Worker 'run-cpu' 10 4096 -BadHash
    $null = Invoke-Worker 'run-cpu' 10 4096 -BadVerifier
    $summary = [ordered]@{
        schema = 'neuro.windows.remote_worker.selftest.v1'; status = 'passed'; checks = $checks
        actual_windows_worker_execution = $true; actual_ssh_execution = $false
        gpu_execution = $false; miner_validation = $false; cases = @($cases.ToArray())
        candidate_identity_checks = $true; canonical_serializer = 'same_staged_Python_on_both_hosts'
    }
    [IO.File]::WriteAllText((Join-Path $evidence 'remote-worker-selftest.json'),
        ($summary | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
    $summary | ConvertTo-Json -Depth 8 -Compress | Write-Output
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
