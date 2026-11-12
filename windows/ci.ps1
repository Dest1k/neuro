# SPDX-License-Identifier: MIT
# Fixed direct Windows build/check actions for the bounded SSH worker.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('build-cpu', 'build-probes', 'run-cpu', 'run-cuda')]
    [string]$Action,
    [ValidateRange(0, 15)][int]$Device = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'These actions require native Windows; WSL is a different execution profile.'
}
if ($PSBoundParameters.ContainsKey('Device') -and $Action -ne 'run-cuda') {
    throw '-Device is only valid for run-cuda.'
}
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Repo
$Evidence = Join-Path $Repo 'artifacts/windows'
$Build = Join-Path $Repo 'build/windows'
$Reference = Join-Path $Repo 'build/proof-target/x86_64-pc-windows-msvc/release/pearl-proof-check.exe'
[IO.Directory]::CreateDirectory($Evidence) | Out-Null
$env:RAYON_NUM_THREADS = '2'
$env:PYTHONUTF8 = '1'
$env:VSLANG = '1033'
# The pinned official source fetch includes deep CUTLASS paths. Use a short,
# isolated cache on this drive; callers may supply another short writable path.
# rustup and its installed toolchains remain in their existing locations.
if (-not $env:CARGO_HOME) { $env:CARGO_HOME = Join-Path ([IO.Path]::GetPathRoot($Repo)) 'nc' }

if (-not ('Neuro.Windows.BoundedProcess' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
}
$Phases = [Collections.Generic.List[object]]::new()
$Record = [ordered]@{
    schema = 'neuro.windows_action.v2'; action = $Action; passed = $false
    started_at = [DateTime]::UtcNow.ToString('o'); windows_execution = $true
    gpu_execution = $false; live_pool = $false; error = $null
    source_fingerprint = $null; source_fingerprint_kind = 'complete_candidate_manifest_v1'
    candidate_files = $null; candidate_bytes = $null
    gpu_handoff = $null; gpu_handoff_error = $null; search_recovery = $null
    artifacts = @{}; phases = $Phases
}
$ReportPath = Join-Path $Evidence ($Action + '.json')
if ($Action -like 'build-*') { $ReportPath = Join-Path $Evidence 'build.json' }

function Save-Record {
    $json = $Record | ConvertTo-Json -Depth 16
    $temporary = $ReportPath + '.tmp'
    [IO.File]::WriteAllText($temporary, $json + "`n", [Text.UTF8Encoding]::new($false))
    # PowerShell coerces an ordinary $null to an empty string for this .NET
    # overload. NullString preserves the required null backup filename.
    if ([IO.File]::Exists($ReportPath)) { [IO.File]::Replace($temporary, $ReportPath, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $ReportPath) }
}

function Invoke-Checked {
    param([string]$Name, [string]$Exe, [string[]]$Arguments, [int]$TimeoutSeconds = 300)
    $resolved = (Get-Command $Exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    Write-Host ('[neuro] ' + $Name)
    $result = [Neuro.Windows.BoundedProcess]::Run($resolved, $Arguments, $Repo, $TimeoutSeconds * 1000, 1048576)
    $entry = [ordered]@{
        name = $Name; argv = @($resolved) + $Arguments; exit_code = $result.ExitCode
        timed_out = $result.TimedOut; output_complete = $result.OutputComplete
        elapsed_ms = $result.DurationMs; error = $result.Error
        stdout = $result.Stdout; stderr = $result.Stderr
        stdout_truncated = $result.StdoutTruncated; stderr_truncated = $result.StderrTruncated
    }
    $Phases.Add($entry)
    if ($result.Stdout) { Write-Host ($result.Stdout.TrimEnd()) }
    if ($result.Stderr) { Write-Host ($result.Stderr.TrimEnd()) }
    Save-Record
    if ($result.Error -or $result.TimedOut -or -not $result.OutputComplete -or
        $result.StdoutTruncated -or $result.StderrTruncated -or $result.ExitCode -ne 0) {
        throw ('Phase failed: ' + $Name + '; exit=' + $result.ExitCode + '; timeout=' + $result.TimedOut + '; ' + $result.Error)
    }
    return $result
}

function Enter-CompilerEnvironment {
    $locator = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    if (-not [IO.File]::Exists($locator)) { throw 'Visual Studio Build Tools and vswhere are required.' }
    $found = Invoke-Checked 'locate Visual Studio x64 tools' $locator @('-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath') 30
    $installation = $found.Stdout.Trim()
    if (-not $installation -or $installation.Contains("`n")) { throw 'Expected one Visual Studio installation.' }
    Import-Module (Join-Path $installation 'Common7/Tools/Microsoft.VisualStudio.DevShell.dll')
    Enter-VsDevShell -VsInstallPath $installation -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
    if ($env:VSCMD_ARG_TGT_ARCH -ne 'x64') { throw 'An x64 MSVC environment is required.' }
}

function Source-Fingerprint {
    $result = Invoke-Checked 'fingerprint complete candidate' $Python @(
        '-I', 'tools/candidate_manifest.py', 'fingerprint', '--root', $Repo) 120
    $payload = $result.Stdout | ConvertFrom-Json
    if ($payload.schema -cne 'neuro.candidate.fingerprint.v1' -or $payload.passed -ne $true -or
        $payload.candidate_sha256 -cnotmatch '^[a-f0-9]{64}$') {
        throw 'Missing complete candidate fingerprint.'
    }
    $Record.candidate_files = $payload.files
    $Record.candidate_bytes = $payload.bytes
    return $payload.candidate_sha256
}

function Binary-Inventory([bool]$Cuda) {
    $required = @('neuro_pearl_proof.exe', 'neuro_pearl_proof_test.exe', 'neuro_pool_replay_client.exe',
        'neuro_mining_job_test.exe', 'neuro_attempt_search_test.exe', 'neuro_search_journal_test.exe',
        'neuro_pool_wire_test.exe', 'neuro_cli_test.exe',
        'neuro_protocol_test.exe', 'neuro_mma_layout_test.exe', 'neuro_fullk_test.exe',
        'neuro_blake3_vectors_test.exe', 'neuro_pearl_alignment_test.exe', 'neuro_pearl_input_test.exe')
    if ($Cuda) { $required += @('neuro_mma_probe.exe', 'neuro_fullk_probe.exe', 'neuro_pearl_probe.exe',
        'neuro_pool_replay_client_cuda.exe', 'neuro_mining_cuda_host_test.exe') }
    foreach ($name in $required) {
        if (-not [IO.File]::Exists((Join-Path $Build $name))) { throw ('Missing built artifact: ' + $name) }
    }
    # Cover every native executable, including new test targets, and the actual
    # official verifier. Build products stay outside the source manifest.
    $files = @(Get-ChildItem -LiteralPath $Build -File -Filter 'neuro_*.exe' |
        ForEach-Object { $_.FullName }) + @($Reference)
    $inventory = @{}
    foreach ($file in $files) {
        $relative = $file.Substring($Repo.Length + 1).Replace('\', '/')
        $inventory[$relative] = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $inventory
}

function Require-MatchingEvidence([string]$Path, [bool]$NeedCuda, [string[]]$ExpectedActions) {
    if (-not [IO.File]::Exists($Path)) { throw ('Missing prior gate: ' + $Path) }
    $prior = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($prior.schema -cne 'neuro.windows_action.v2' -or
        $prior.source_fingerprint_kind -cne 'complete_candidate_manifest_v1' -or
        $prior.windows_execution -ne $true -or $ExpectedActions -cnotcontains $prior.action -or
        $prior.passed -ne $true -or $prior.source_fingerprint -ne $Record.source_fingerprint) {
        throw ('Prior gate failed or its source changed: ' + $Path)
    }
    if ($NeedCuda -and $prior.cuda_enabled -ne $true) { throw 'Build evidence has no CUDA backend.' }
    $current = Binary-Inventory ([bool]$prior.cuda_enabled)
    $properties = @($prior.artifacts.PSObject.Properties)
    if ($properties.Count -ne $current.Count) { throw 'Built executable inventory changed after its gate.' }
    foreach ($property in $properties) {
        if (-not $current.ContainsKey($property.Name) -or $current[$property.Name] -cne $property.Value) {
            throw ('Built artifact changed after its gate: ' + $property.Name)
        }
    }
    return $prior
}

Save-Record
try {
    if ([IO.File]::ReadAllBytes((Join-Path $Repo 'README.md')).Length -ne 0) { throw 'README must remain empty.' }
    $Python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $null = Invoke-Checked 'verify pinned sources' $Python @('tools/neuroctl.py', 'source-check') 60
    $Record.source_fingerprint = Source-Fingerprint
    if ($Action -like 'build-*') {
        Enter-CompilerEnvironment
        $withCuda = $Action -eq 'build-probes'
        if ($withCuda) {
            $null = Invoke-Checked 'CUDA compiler inventory' 'nvcc' @('--version') 30
        }
        $null = Invoke-Checked 'real Rust proof build and unit tests' $Python @('tools/build_pearl_proof.py', '--target', 'x86_64-pc-windows-msvc') 1800
        $cudaOption = if ($withCuda) { 'ON' } else { 'OFF' }
        $configure = @('-S', '.', '-B', 'build/windows', '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                       '-DCMAKE_C_COMPILER=cl', '-DCMAKE_CXX_COMPILER=cl', '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL',
                       '-DNEURO_ENABLE_PROOF=ON', '-DNEURO_BUILD_CPU_CHECKS=ON', ('-DNEURO_ENABLE_CUDA=' + $cudaOption))
        if ($withCuda) { $configure += '-DCMAKE_CUDA_ARCHITECTURES=120' }
        $null = Invoke-Checked 'configure native Windows build' 'cmake' $configure 180
        $null = Invoke-Checked 'compile native Windows targets' 'cmake' @('--build', 'build/windows', '--parallel', '2') 900
        $Record.cuda_enabled = $withCuda
        $Record.artifacts = Binary-Inventory $withCuda
    } else {
        $buildEvidence = Require-MatchingEvidence (Join-Path $Evidence 'build.json') ($Action -eq 'run-cuda') @('build-cpu', 'build-probes')
        $Record.cuda_enabled = $buildEvidence.cuda_enabled
        $Record.artifacts = Binary-Inventory $buildEvidence.cuda_enabled
        if ($Action -eq 'run-cpu') {
            $null = Invoke-Checked 'Python controls' $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v') 180
            $null = Invoke-Checked 'native Windows CPU tests' 'ctest' @('--test-dir', 'build/windows', '-C', 'Release', '--output-on-failure', '--no-tests=error') 180
            $null = Invoke-Checked 'official proof corruption corpus' $Python @('tools/check_pearl_proofs.py', '--output', 'artifacts/windows/proof-check.json') 180
            $null = Invoke-Checked 'real Windows loopback lifecycle' $Python @('tools/replay_pool.py', '--backend', 'cpu', '--output', 'artifacts/windows/pool-replay.json') 180
            $null = Invoke-Checked 'durable Windows search restart recovery' $Python @('tools/replay_recovery.py',
                '--native', 'build/windows/neuro_pool_replay_client.exe', '--reference', $Reference,
                '--backend', 'cpu', '--work-dir', 'artifacts/windows/search-recovery',
                '--output', 'artifacts/windows/search-recovery.json') 180
            $recoveryPath = Join-Path $Evidence 'search-recovery.json'
            $recovery = Get-Content -LiteralPath $recoveryPath -Raw | ConvertFrom-Json
            if ($recovery.schema -cne 'neuro.pool_recovery.v1' -or $recovery.passed -ne $true -or
                $recovery.windows_execution -ne $true -or $recovery.cpu_execution -ne $true -or
                $recovery.gpu_execution -ne $false -or $recovery.external_pool_connection -ne $false -or
                $recovery.live_pool_acceptance -ne $false) {
                throw 'Search recovery has no successful native Windows CPU observation.'
            }
            $Record.search_recovery = [ordered]@{
                path = 'artifacts/windows/search-recovery.json'
                sha256 = (Get-FileHash -LiteralPath $recoveryPath -Algorithm SHA256).Hash.ToLowerInvariant()
                schema = $recovery.schema; passed = $recovery.passed
                scenarios_run = $recovery.scenarios_run; process_phases_run = $recovery.process_phases_run
                submitted_proofs_checked = $recovery.submitted_proofs_checked
            }
        } else {
            $null = Require-MatchingEvidence (Join-Path $Evidence 'run-cpu.json') $true @('run-cpu')
            . (Join-Path $PSScriptRoot 'gpu_lease.ps1')
            $gpuLease = $null
            $gpuFailure = $null
            try {
                # Stop the configured owner miner only after its interactive
                # restoration watchdog is ready. CPU/build actions never lease it.
                $gpuLease = Enter-NeuroGpuLease -RepositoryRoot $Repo -Device $Device
                foreach ($name in @('neuro_mma_probe.exe', 'neuro_fullk_probe.exe', 'neuro_pearl_probe.exe')) {
                    $run = Invoke-Checked ('GPU correctness ' + $name) (Join-Path $Build $name) @('--device', [string]$Device) 60
                    $payload = $run.Stdout | ConvertFrom-Json
                    if ($payload.status -ne 'PASS' -or $payload.exact -ne $true -or $payload.gpu_execution_observed -ne $true) {
                        throw ('Missing exact physical GPU evidence from ' + $name)
                    }
                    $Record.gpu_execution = $true
                }
                $null = Invoke-Checked 'GPU loopback lifecycle' $Python @('tools/replay_pool.py', '--native',
                    'build/windows/neuro_pool_replay_client_cuda.exe', '--backend', 'cuda', '--device', [string]$Device,
                    '--work-dir', 'artifacts/windows/pool-replay-gpu', '--output', 'artifacts/windows/pool-replay-gpu.json') 300
                $gpuReplay = Get-Content (Join-Path $Evidence 'pool-replay-gpu.json') -Raw | ConvertFrom-Json
                if ($gpuReplay.passed -ne $true -or $gpuReplay.gpu_execution -ne $true) { throw 'GPU replay has no verified execution.' }
            } catch {
                $gpuFailure = $_.Exception.Message
            } finally {
                if ($null -ne $gpuLease) {
                    try {
                        # Every bounded child job has completed/been terminated
                        # before release. The independent watcher also checks
                        # that known GPU executables are absent before restart.
                        $Record.gpu_handoff = Exit-NeuroGpuLease -Lease $gpuLease
                        if ($Record.gpu_handoff.passed -ne $true) { throw 'Owner GPU handoff has no successful restoration verdict.' }
                    } catch {
                        $Record.gpu_handoff_error = $_.Exception.Message
                    }
                    Save-Record
                }
            }
            if ($gpuFailure -or $Record.gpu_handoff_error) {
                throw ((@($gpuFailure, $Record.gpu_handoff_error) | Where-Object { $_ }) -join '; ')
            }
        }
    }
    if ((Source-Fingerprint) -ne $Record.source_fingerprint) { throw 'Source changed during the action.' }
    $finalArtifacts = Binary-Inventory ([bool]$Record.cuda_enabled)
    if ($finalArtifacts.Count -ne $Record.artifacts.Count) { throw 'Built executable inventory changed during the action.' }
    foreach ($path in $Record.artifacts.Keys) {
        if (-not $finalArtifacts.ContainsKey($path) -or $finalArtifacts[$path] -cne $Record.artifacts[$path]) {
            throw ('Built artifact changed during the action: ' + $path)
        }
    }
    $Record.passed = $true
} catch {
    $Record.error = $_.Exception.Message
    # Cargo writes full logs to artifacts. Surface only the failing stage's
    # bounded tail so an unattended Windows build can diagnose the real error.
    try {
        $proofReportPath = Join-Path $Repo 'artifacts/proof-build.json'
        if ([IO.File]::Exists($proofReportPath)) {
            $proofBuild = Get-Content -LiteralPath $proofReportPath -Raw | ConvertFrom-Json
            if ($proofBuild.PSObject.Properties.Name -contains 'commands') {
                foreach ($stage in $proofBuild.commands) {
                    if (-not $stage.passed -and $stage.log.StartsWith('artifacts/proof-build/')) {
                        $log = Join-Path $Repo $stage.log
                        if ([IO.File]::Exists($log)) {
                            $tail = (Get-Content -LiteralPath $log -Tail 80) -join "`n"
                            if ($tail.Length -gt 16384) { $tail = $tail.Substring($tail.Length - 16384) }
                            Write-Host ('[neuro] failing Rust stage: ' + $stage.name)
                            Write-Host $tail
                        }
                    }
                }
            }
        }
    } catch { Write-Host '[neuro] Detailed build log is retained under artifacts/proof-build.' }
} finally {
    $Record.finished_at = [DateTime]::UtcNow.ToString('o')
    Save-Record
}
$summary = [ordered]@{
    schema = $Record.schema; action = $Record.action; passed = $Record.passed
    windows_execution = $Record.windows_execution; gpu_execution = $Record.gpu_execution; error = $Record.error
}
Write-Output ($summary | ConvertTo-Json -Compress)
if (-not $Record.passed) { exit 1 }
exit 0
