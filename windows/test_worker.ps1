# SPDX-License-Identifier: MIT
# Real Win32 execution; no SSH, GPU, installer, or global process termination.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -Path (Join-Path $PSScriptRoot 'BoundedProcess.cs')
$shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('neuro worker test ' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
$checks = 0

function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Worker self-test failed: $Message" }
    $script:checks++
}
function Encode([string]$Text) {
    [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Text))
}
function Run([string]$Code, [int]$Timeout = 10000, [int]$Cap = 4096) {
    [Neuro.Windows.BoundedProcess]::Run($shell,
        @('-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', (Encode $Code)),
        $temporary, $Timeout, $Cap)
}
function Assert-Gone([string]$PidFile) {
    Check (Test-Path -LiteralPath $PidFile -PathType Leaf) 'descendant was actually started'
    $childId = [int][IO.File]::ReadAllText($PidFile)
    $alive = $false
    try {
        $process = [Diagnostics.Process]::GetProcessById($childId)
        $alive = -not $process.HasExited
        $process.Dispose()
    } catch [ArgumentException] { $alive = $false }
    Check (-not $alive) 'owned descendant exited with its job'
}

try {
    $ok = Run "[Console]::Write('hello');[Console]::Error.Write('warning');exit 0"
    Check ($ok.ExitCode -eq 0 -and $ok.Error -eq '' -and -not $ok.TimedOut) 'successful exit'
    Check ($ok.OutputComplete -and $ok.Stdout -ceq 'hello' -and $ok.Stderr -ceq 'warning') 'both output pipes'
    Check ($ok.StdoutBytes -eq 5 -and $ok.StderrBytes -eq 7) 'byte accounting'

    $bad = Run "[Console]::Write('nonzero');exit 7"
    Check ($bad.ExitCode -eq 7 -and $bad.OutputComplete -and -not $bad.TimedOut) 'nonzero exit is retained'
    $missing = [Neuro.Windows.BoundedProcess]::Run((Join-Path $temporary 'absent.exe'),
        @(), $temporary, 1000, 1024)
    Check ($missing.Error -ne '' -and $missing.ExitCode -ne 0 -and $missing.ProcessId -eq 0) 'launch failure'

    $large = Run "[Console]::Write(('x' * 32768));[Console]::Error.Write(('y' * 17000));exit 0" 10000 1024
    Check ($large.ExitCode -eq 0 -and $large.OutputComplete) 'large streams drain without deadlock'
    Check ($large.Stdout.Length -eq 1024 -and $large.Stderr.Length -eq 1024) 'retained output bound'
    Check ($large.StdoutBytes -eq 32768 -and $large.StderrBytes -eq 17000) 'discarded bytes still counted'
    Check ($large.StdoutTruncated -and $large.StderrTruncated) 'truncation is explicit'

    $echoScript = Join-Path $temporary 'echo arguments.ps1'
    [IO.File]::WriteAllText($echoScript, @'
param([string]$A, [string]$B, [string]$C, [string]$D)
[Console]::Write((@($A, $B, $C, $D) | ConvertTo-Json -Compress))
'@)
    $arguments = @('alpha beta', 'q"uote', 'C:\ends\', '')
    $quoted = [Neuro.Windows.BoundedProcess]::Run($shell,
        (@('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $echoScript) + $arguments),
        $temporary, 10000, 4096)
    Check ($quoted.ExitCode -eq 0 -and $quoted.OutputComplete) 'path with spaces executes'
    # @() wraps the one array object that Windows PowerShell 5.1 emits, so
    # Count becomes 1 even though all four arguments arrived.
    $decoded = $quoted.Stdout | ConvertFrom-Json
    Check ($decoded -is [Array] -and $decoded.Length -eq 4) 'argument count including empty string'
    for ($i = 0; $i -lt 4; $i++) { Check ($decoded[$i] -ceq $arguments[$i]) "literal argument $i" }

    $sleepEncoded = Encode 'Start-Sleep -Seconds 30'
    foreach ($mode in @('timeout', 'parent-exit')) {
        $pidFile = Join-Path $temporary ($mode + '.pid')
        $escapedShell = $shell.Replace("'", "''")
        $escapedPidFile = $pidFile.Replace("'", "''")
        $spawn = @"
`$info = New-Object Diagnostics.ProcessStartInfo
`$info.FileName = '$escapedShell'
`$info.Arguments = '-NoLogo -NoProfile -NonInteractive -EncodedCommand $sleepEncoded'
`$info.UseShellExecute = `$false
`$info.CreateNoWindow = `$true
`$child = [Diagnostics.Process]::Start(`$info)
[IO.File]::WriteAllText('$escapedPidFile', [string]`$child.Id)
"@
        if ($mode -eq 'timeout') { $spawn += "`nStart-Sleep -Seconds 30" }
        else { $spawn += "`nexit 0" }
        $tree = Run $spawn 3000 4096
        if ($mode -eq 'timeout') {
            Check ($tree.TimedOut -and $tree.ExitCode -ne 0) 'remote deadline expires'
        } else {
            Check (-not $tree.TimedOut -and $tree.ExitCode -eq 0) 'leader can exit before descendant'
        }
        Check ($tree.Error -eq '' -and $tree.OutputComplete -and $tree.DurationMs -lt 10000) 'bounded job cleanup and pipe EOF'
        Assert-Gone $pidFile
    }
    [ordered]@{
        schema = 'neuro.windows.worker.selftest.v1'
        status = 'passed'
        checks = $checks
        actual_windows_execution = $true
        gpu_execution = $false
    } | ConvertTo-Json -Compress | Write-Output
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
