# SPDX-License-Identifier: MIT
# Fixed build/check actions shared by Windows CI and the bounded SSH worker.
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

if (-not ('Neuro.Windows.BoundedProcess' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
}
$Phases = [Collections.Generic.List[object]]::new()
$Record = [ordered]@{
    schema = 'neuro.windows_action.v1'; action = $Action; passed = $false
    started_at = [DateTime]::UtcNow.ToString('o'); windows_execution = $true
    gpu_execution = $false; live_pool = $false; error = $null
    source_fingerprint = $null; artifacts = @{}; phases = $Phases
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
    $paths = [Collections.Generic.List[string]]::new()
    foreach ($directory in @('native', 'cmake')) {
        Get-ChildItem (Join-Path $Repo $directory) -File -Recurse | ForEach-Object { $paths.Add($_.FullName) }
    }
    foreach ($name in @('CMakeLists.txt', 'configs/proof-build.json', 'tools/build_pearl_proof.py',
                         'tools/check_pearl_proofs.py', 'tools/replay_pool.py', 'windows/ci.ps1')) {
        $paths.Add((Join-Path $Repo $name))
    }
    $rows = foreach ($path in ($paths | Sort-Object)) {
        $relative = $path.Substring($Repo.Length + 1).Replace('\', '/')
        $relative + ':' + (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $digest = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($rows -join "`n"))
        return ([BitConverter]::ToString($digest.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $digest.Dispose() }
}

function Binary-Inventory([bool]$Cuda) {
    $names = @('neuro_pearl_proof.exe', 'neuro_pool_replay_client.exe')
    if ($Cuda) { $names += @('neuro_mma_probe.exe', 'neuro_fullk_probe.exe', 'neuro_pearl_probe.exe', 'neuro_pool_replay_client_cuda.exe') }
    $files = @($names | ForEach-Object { Join-Path $Build $_ }) + @($Reference)
    $inventory = @{}
    foreach ($file in $files) {
        $relative = $file.Substring($Repo.Length + 1).Replace('\', '/')
        $inventory[$relative] = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $inventory
}

function Require-MatchingEvidence([string]$Path, [bool]$NeedCuda) {
    if (-not [IO.File]::Exists($Path)) { throw ('Missing prior gate: ' + $Path) }
    $prior = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($prior.passed -ne $true -or $prior.source_fingerprint -ne $Record.source_fingerprint) {
        throw ('Prior gate failed or its source changed: ' + $Path)
    }
    if ($NeedCuda -and $prior.cuda_enabled -ne $true) { throw 'Build evidence has no CUDA backend.' }
    foreach ($property in $prior.artifacts.PSObject.Properties) {
        $file = Join-Path $Repo $property.Name
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $property.Value) {
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
        $buildEvidence = Require-MatchingEvidence (Join-Path $Evidence 'build.json') ($Action -eq 'run-cuda')
        $Record.cuda_enabled = $buildEvidence.cuda_enabled
        $Record.artifacts = Binary-Inventory $buildEvidence.cuda_enabled
        if ($Action -eq 'run-cpu') {
            $null = Invoke-Checked 'Python controls' $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v') 180
            $null = Invoke-Checked 'native Windows CPU tests' 'ctest' @('--test-dir', 'build/windows', '-C', 'Release', '--output-on-failure') 180
            $null = Invoke-Checked 'official proof corruption corpus' $Python @('tools/check_pearl_proofs.py', '--output', 'artifacts/windows/proof-check.json') 180
            $null = Invoke-Checked 'real Windows loopback lifecycle' $Python @('tools/replay_pool.py', '--backend', 'cpu', '--output', 'artifacts/windows/pool-replay.json') 180
        } else {
            $null = Require-MatchingEvidence (Join-Path $Evidence 'run-cpu.json') $true
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
        }
    }
    if ((Source-Fingerprint) -ne $Record.source_fingerprint) { throw 'Source changed during the action.' }
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
