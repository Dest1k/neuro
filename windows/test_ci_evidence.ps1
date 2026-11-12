# SPDX-License-Identifier: MIT
# Exercise the actual prerequisite functions with synthetic reports/files only.
# No ci.ps1 action, executable, miner, owner connection or GPU job is started.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sourcePath = Join-Path $PSScriptRoot 'ci.ps1'
$outputDirectory = Join-Path $repositoryRoot 'artifacts/windows'
$outputPath = Join-Path $outputDirectory 'ci-evidence-selftest.json'
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('neuro evidence fixture ' + [Guid]::NewGuid().ToString('N'))
$clock = [Diagnostics.Stopwatch]::StartNew()
$script:EvidenceChecks = 0
$script:EvidenceCases = [Collections.Generic.List[object]]::new()
$result = [ordered]@{
    schema = 'neuro.windows.ci_evidence.selftest.v1'; status = 'failed'; passed = $false
    checks = 0; actual_windows_execution = ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT)
    synthetic_evidence = $true; synthetic_binaries = $true; actions_executed = $false
    miner_validation = $false; actual_owner_connection = $false; physical_gpu_execution = $false
    production_source_sha256 = $null; extracted_functions = @('Binary-Inventory', 'Require-MatchingEvidence')
    cases = $script:EvidenceCases; elapsed_ms = 0; error = $null; cleanup_error = $null
}

function Assert-Evidence([bool]$Condition, [string]$Name, [string]$Detail = '') {
    if ($clock.Elapsed.TotalSeconds -gt 30) { throw 'CI evidence fixtures exceeded their 30-second bound.' }
    if (-not $Condition) { throw ('CI evidence fixture failed: ' + $Name + '; ' + $Detail) }
    $script:EvidenceChecks++
    $script:EvidenceCases.Add([ordered]@{ name = $Name; passed = $true; detail = $Detail })
}
function Assert-EvidenceReject([scriptblock]$Body, [string]$Name, [string]$ExpectedMessage) {
    $failure = $null
    try { $null = & $Body } catch { $failure = $_.Exception.Message }
    Assert-Evidence ($null -ne $failure -and $failure -like $ExpectedMessage) $Name ([string]$failure)
}
function Write-Evidence($Value) {
    [IO.File]::WriteAllText($fixtureReport, ($Value | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
}
function New-Evidence([string]$Action, [bool]$Cuda) {
    return [pscustomobject][ordered]@{
        schema = 'neuro.windows_action.v2'; action = $Action; passed = $true; windows_execution = $true
        source_fingerprint_kind = 'complete_candidate_manifest_v1'; source_fingerprint = $Record.source_fingerprint
        cuda_enabled = $Cuda; artifacts = (Binary-Inventory $Cuda)
    }
}
function Copy-Evidence($Value) {
    return ($Value | ConvertTo-Json -Depth 8 | ConvertFrom-Json)
}
function Assert-EvidenceAccept($Value, [bool]$NeedCuda, [string[]]$Actions, [string]$Name) {
    Write-Evidence $Value
    $accepted = Require-MatchingEvidence $fixtureReport $NeedCuda $Actions
    Assert-Evidence ($accepted.action -ceq $Value.action -and
        $accepted.source_fingerprint -ceq $Record.source_fingerprint) $Name
}
function Assert-ReportReject($Value, [bool]$NeedCuda, [string[]]$Actions, [string]$Name,
                             [string]$ExpectedMessage = 'Prior gate failed or its source changed:*') {
    Write-Evidence $Value
    Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $NeedCuda $Actions } $Name $ExpectedMessage
}

try {
    if (-not $result.actual_windows_execution) { throw 'CI evidence fixtures require native Windows.' }
    if ((Get-Item -LiteralPath $sourcePath).Length -gt 1048576) { throw 'Unexpectedly large ci.ps1 source.' }
    $result.production_source_sha256 = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $tokens = $null; $parseErrors = $null
    $sourceAst = [Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw ('Cannot parse production ci.ps1: ' + $parseErrors[0].Message) }
    foreach ($functionName in $result.extracted_functions) {
        $definitions = @($sourceAst.FindAll({ param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $functionName
        }, $false))
        if ($definitions.Count -ne 1) { throw ('Expected exactly one production function: ' + $functionName) }
        # Dot-source only this parsed function definition, never the action body.
        . ([scriptblock]::Create($definitions[0].Extent.Text))
    }

    # Function variables deliberately refer only to this disposable checkout.
    $Repo = Join-Path $temporary 'repo'
    $Build = Join-Path $Repo 'build/windows'
    $Reference = Join-Path $Repo 'build/proof-target/x86_64-pc-windows-msvc/release/pearl-proof-check.exe'
    $Record = [ordered]@{ source_fingerprint = ('a' * 64) }
    $fixtureReport = Join-Path $temporary 'run-cpu.json'
    [IO.Directory]::CreateDirectory($Build) | Out-Null
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Reference)) | Out-Null
    $binaryNames = @('neuro_pearl_proof.exe', 'neuro_pearl_proof_test.exe', 'neuro_pool_replay_client.exe',
        'neuro_mining_job_test.exe', 'neuro_attempt_search_test.exe', 'neuro_search_journal_test.exe',
        'neuro_pool_wire_test.exe', 'neuro_cli_test.exe',
        'neuro_protocol_test.exe', 'neuro_mma_layout_test.exe', 'neuro_fullk_test.exe',
        'neuro_blake3_vectors_test.exe', 'neuro_pearl_alignment_test.exe', 'neuro_pearl_input_test.exe',
        'neuro_mma_probe.exe', 'neuro_fullk_probe.exe', 'neuro_pearl_probe.exe',
        'neuro_pool_replay_client_cuda.exe', 'neuro_mining_cuda_host_test.exe',
        'neuro_additional_fixture.exe')
    foreach ($name in $binaryNames) {
        [IO.File]::WriteAllText((Join-Path $Build $name), ('SYNTHETIC NON-EXECUTABLE FIXTURE: ' + $name), [Text.Encoding]::ASCII)
    }
    [IO.File]::WriteAllText($Reference, 'SYNTHETIC NON-EXECUTABLE OFFICIAL-CHECKER FIXTURE', [Text.Encoding]::ASCII)

    $buildActions = @('build-cpu', 'build-probes')
    $cpuAction = @('run-cpu')
    $cpuBuild = New-Evidence 'build-cpu' $false
    $cudaBuild = New-Evidence 'build-probes' $true
    $cpuGate = New-Evidence 'run-cpu' $true
    Assert-EvidenceAccept $cpuBuild $false $buildActions 'matching CPU build evidence accepted'
    Assert-EvidenceAccept $cudaBuild $true $buildActions 'matching CUDA build evidence accepted'
    Assert-EvidenceAccept $cpuGate $true $cpuAction 'matching CPU prerequisite accepted'
    Assert-ReportReject $cudaBuild $true $cpuAction 'build report renamed run-cpu.json rejected'
    Assert-ReportReject $cpuGate $true $buildActions 'CPU run report cannot substitute for build evidence'

    $changed = Copy-Evidence $cpuGate; $changed.windows_execution = $false
    Assert-ReportReject $changed $true $cpuAction 'non-Windows evidence rejected'
    $changed = Copy-Evidence $cpuGate; $changed.passed = $false
    Assert-ReportReject $changed $true $cpuAction 'failed prerequisite rejected'
    $changed = Copy-Evidence $cpuGate; $changed.source_fingerprint = 'b' * 64
    Assert-ReportReject $changed $true $cpuAction 'stale source identity rejected'
    $changed = Copy-Evidence $cpuGate; $changed.schema = 'neuro.windows_action.v1'
    Assert-ReportReject $changed $true $cpuAction 'legacy evidence schema rejected'
    $changed = Copy-Evidence $cpuGate; $changed.source_fingerprint_kind = 'partial_source_fixture'
    Assert-ReportReject $changed $true $cpuAction 'partial source fingerprint rejected'
    Assert-ReportReject $cpuBuild $true $buildActions 'CPU-only build cannot authorize GPU action' 'Build evidence has no CUDA backend.'
    Assert-EvidenceReject { Require-MatchingEvidence (Join-Path $temporary 'absent.json') $true $cpuAction } `
        'missing prerequisite report rejected' 'Missing prior gate:*'

    Write-Evidence $cpuGate
    $requiredPath = Join-Path $Build 'neuro_pearl_proof.exe'
    $requiredBytes = [IO.File]::ReadAllBytes($requiredPath)
    try {
        [IO.File]::AppendAllText($requiredPath, ' changed')
        Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
            'changed required executable rejected' 'Built artifact changed after its gate:*'
    } finally { [IO.File]::WriteAllBytes($requiredPath, $requiredBytes) }
    try {
        [IO.File]::Delete($requiredPath)
        Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
            'missing required executable rejected' 'Missing built artifact:*'
    } finally { [IO.File]::WriteAllBytes($requiredPath, $requiredBytes) }

    foreach ($requiredSearchName in @('neuro_attempt_search_test.exe', 'neuro_search_journal_test.exe')) {
        $requiredSearchPath = Join-Path $Build $requiredSearchName
        $requiredSearchBytes = [IO.File]::ReadAllBytes($requiredSearchPath)
        try {
            [IO.File]::Delete($requiredSearchPath)
            Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
                ('missing required search executable rejected: ' + $requiredSearchName) 'Missing built artifact:*'
        } finally { [IO.File]::WriteAllBytes($requiredSearchPath, $requiredSearchBytes) }
    }

    $additionalPath = Join-Path $Build 'neuro_additional_fixture.exe'
    $additionalBytes = [IO.File]::ReadAllBytes($additionalPath)
    try {
        [IO.File]::AppendAllText($additionalPath, ' changed')
        Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
            'changed additional native target rejected' 'Built artifact changed after its gate:*'
    } finally { [IO.File]::WriteAllBytes($additionalPath, $additionalBytes) }
    try {
        [IO.File]::Delete($additionalPath)
        Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
            'removed additional native target rejected' 'Built executable inventory changed after its gate.'
    } finally { [IO.File]::WriteAllBytes($additionalPath, $additionalBytes) }
    $newPath = Join-Path $Build 'neuro_extra_fixture.exe'
    try {
        [IO.File]::WriteAllText($newPath, 'SYNTHETIC EXTRA FIXTURE', [Text.Encoding]::ASCII)
        Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
            'new native executable absent from evidence rejected' 'Built executable inventory changed after its gate.'
    } finally { if ([IO.File]::Exists($newPath)) { [IO.File]::Delete($newPath) } }
    $referenceBytes = [IO.File]::ReadAllBytes($Reference)
    try {
        [IO.File]::AppendAllText($Reference, ' changed')
        Assert-EvidenceReject { Require-MatchingEvidence $fixtureReport $true $cpuAction } `
            'changed official verifier rejected' 'Built artifact changed after its gate:*'
    } finally { [IO.File]::WriteAllBytes($Reference, $referenceBytes) }

    $requiredKey = 'build/windows/neuro_pearl_proof.exe'
    $changed = Copy-Evidence $cpuGate; $changed.artifacts.PSObject.Properties.Remove($requiredKey)
    Assert-ReportReject $changed $true $cpuAction 'missing inventory record rejected' 'Built executable inventory changed after its gate.'
    $changed = Copy-Evidence $cpuGate; $changed.artifacts.PSObject.Properties[$requiredKey].Value = 'c' * 64
    Assert-ReportReject $changed $true $cpuAction 'stale recorded binary hash rejected' 'Built artifact changed after its gate:*'
    $changed = Copy-Evidence $cpuGate
    $changed.artifacts | Add-Member -MemberType NoteProperty -Name 'build/windows/neuro_unbuilt_fixture.exe' -Value ('d' * 64)
    Assert-ReportReject $changed $true $cpuAction 'extra inventory record rejected' 'Built executable inventory changed after its gate.'
    Assert-EvidenceAccept $cpuGate $true $cpuAction 'original prerequisite accepted after exact fixture restoration'
    Assert-Evidence ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant() -ceq
        $result.production_source_sha256) 'production gate source unchanged during fixtures'
    $result.status = 'passed'; $result.passed = $true
} catch {
    $result.error = $_.Exception.Message
} finally {
    try {
        if ([IO.Directory]::Exists($temporary)) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    } catch {
        $result.cleanup_error = $_.Exception.Message; $result.status = 'failed'; $result.passed = $false
    }
    $result.checks = $script:EvidenceChecks; $result.elapsed_ms = $clock.ElapsedMilliseconds
    [IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
    [IO.File]::WriteAllText($outputPath, ($result | ConvertTo-Json -Depth 8) + "`n", [Text.UTF8Encoding]::new($false))
}
Write-Output ($result | ConvertTo-Json -Depth 8 -Compress)
if (-not $result.passed) { exit 1 }
exit 0
