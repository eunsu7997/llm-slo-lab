<#
.SYNOPSIS
    LLM-SLO Lab quality gate: runs the full pytest suite and the real-Uvicorn TCP smoke test,
    records the environment and logs under reports/<timestamp>/, and prints PASS / FAIL / UNVERIFIED.

.DESCRIPTION
    Verdict rules are defined in docs/quality-gates.md. Verdicts come only from real exit codes
    and the evidence files those commands produce.
    Exit codes: 0 = PASS, 1 = FAIL, 2 = UNVERIFIED.
    Precedence FAIL > UNVERIFIED > PASS is applied on every path, including internal errors.
    summary.json is written last and atomically; a missing summary.json or "complete": false is never PASS.
    This script never creates, deletes or modifies the virtual environment.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1
#>
# No [CmdletBinding()] and only string/switch parameters: bad or unknown arguments must not fail
# parameter binding (which would exit 1 = FAIL before this script runs). They are rejected below as UNVERIFIED.
param(
    [string]$ProjectRoot = "",
    [string]$Python = "",
    [string]$ReportRoot = "",
    [string]$MinTests = "223",
    [string]$MinSmokeChecks = "17",
    [switch]$SkipPytest,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding $false
$RequiredGates = @('preflight', 'pytest', 'smoke')
$gates = [ordered]@{}
$ReportDir = $null

function Write-Text([string]$Path, [string]$Text) { [System.IO.File]::WriteAllText($Path, $Text, $Utf8) }

function New-Gate([string]$Status, $ExitCode, [string]$Reason, [string]$Log) {
    [ordered]@{ status = $Status; exit_code = $ExitCode; reason = $Reason; log = $Log }
}

function Get-Verdict {
    # FAIL > UNVERIFIED > PASS over every recorded gate (including 'harness'/'report'),
    # and a required gate that was never recorded counts as UNVERIFIED.
    $statuses = @($RequiredGates | ForEach-Object { if ($gates.Contains($_)) { $gates[$_].status } else { 'UNVERIFIED' } })
    $statuses += @($gates.Keys | Where-Object { $RequiredGates -notcontains $_ } | ForEach-Object { $gates[$_].status })
    if ($statuses -contains 'FAIL') { return @{ verdict = 'FAIL'; exit = 1 } }
    if (@($statuses | Where-Object { $_ -ne 'PASS' }).Count -gt 0) { return @{ verdict = 'UNVERIFIED'; exit = 2 } }
    return @{ verdict = 'PASS'; exit = 0 }
}

function ConvertTo-SummaryJson($Result, [bool]$Complete) {
    [ordered]@{ verdict = $Result.verdict; exit_code = $Result.exit; complete = $Complete; report_dir = $ReportDir
                min_tests = $MinTests; min_smoke_checks = $MinSmokeChecks; gates = $gates } | ConvertTo-Json -Depth 5
}

function Save-Summary($Result) {
    # Both files are written to .tmp first; summary.json is moved into place LAST,
    # so its presence (with "complete": true) means the whole report was saved.
    $md = @("# Verification summary", "", "- Verdict: **$($Result.verdict)** (exit $($Result.exit))", "- Report: ``$ReportDir``",
        "- Mock latency only; no TTFT/TPOT is measured.", "", "| Gate | Status | Exit | Reason |", "|---|---|---|---|")
    foreach ($g in $gates.Keys) {
        $x = $gates[$g]
        $md += "| $g | $($x.status) | $(if ($null -eq $x.exit_code) { '-' } else { $x.exit_code }) | $($x.reason -replace '\|', '/') |"
    }
    $mdPath = Join-Path $ReportDir 'summary.md'
    $jsonPath = Join-Path $ReportDir 'summary.json'
    Write-Text "$mdPath.tmp" ($md -join "`r`n")
    Write-Text "$jsonPath.tmp" (ConvertTo-SummaryJson $Result $true)
    Move-Item -Force "$mdPath.tmp" $mdPath
    Move-Item -Force "$jsonPath.tmp" $jsonPath
}

function Complete-Run {
    # Final step on every path. Never returns PASS unless the full report was saved.
    $result = Get-Verdict
    try {
        if (-not $ReportDir) { throw 'report directory was not created' }
        Save-Summary $result
    } catch {
        $gates.report = New-Gate 'UNVERIFIED' $null "report could not be saved completely: $($_.Exception.Message)" $null
        $result = Get-Verdict
        try {
            Get-ChildItem -Path $ReportDir -Filter 'summary.*.tmp' -ErrorAction SilentlyContinue | Remove-Item -Force
            Write-Text (Join-Path $ReportDir 'summary.json') (ConvertTo-SummaryJson $result $false)
        } catch { Write-Host "warning: fallback summary.json could not be written either: $($_.Exception.Message)" }
    }
    Write-Host ""
    foreach ($g in $gates.Keys) { Write-Host ("{0,-10} {1,-11} {2}" -f $g, $gates[$g].status, $gates[$g].reason) }
    Write-Host ""
    Write-Host "VERDICT: $($result.verdict) (exit $($result.exit))  report: $ReportDir"
    exit $result.exit
}

# Any unexpected error is a harness problem (UNVERIFIED) but must not hide an earlier FAIL.
trap {
    $gates.harness = New-Gate 'UNVERIFIED' $null "verify.ps1 internal error: $($_.Exception.Message)" $null
    Complete-Run
}

$script:LaunchError = $null
function Invoke-Logged {
    # Runs a native command, writes stdout+stderr to $LogPath, returns the exit code,
    # or $null when the process could not be started at all ($script:LaunchError holds the reason).
    param([string]$Exe, [string[]]$Arguments, [string]$LogPath)
    $out = "$LogPath.stdout.tmp"; $err = "$LogPath.stderr.tmp"
    $argLine = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $script:LaunchError = $null
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $argLine -WorkingDirectory $ProjectRoot `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        $code = $p.ExitCode
    } catch {
        $code = $null
        $script:LaunchError = $_.Exception.Message
    }
    $text = "> $Exe $argLine`r`n"
    if (Test-Path $out) { $text += [System.IO.File]::ReadAllText($out); Remove-Item $out }
    if (Test-Path $err) {
        $e = [System.IO.File]::ReadAllText($err); Remove-Item $err
        if ($e) { $text += "`r`n--- stderr ---`r`n$e" }
    }
    if ($script:LaunchError) { $text += "`r`n--- launch error ---`r`n$($script:LaunchError)" }
    $text += "`r`n--- exit code: $(if ($null -eq $code) { 'n/a (not started)' } else { $code }) ---`r`n"
    Write-Text $LogPath $text
    return $code
}

# ---- 0. Arguments and report directory --------------------------------------------------------
# $PSScriptRoot is not available in param() defaults on Windows PowerShell 5.1.
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
if (-not $Python) { $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe' }
if (-not $ReportRoot) { $ReportRoot = Join-Path $ProjectRoot 'reports' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$candidate = Join-Path $ReportRoot $stamp
$n = 1
while (Test-Path $candidate) { $candidate = Join-Path $ReportRoot "$stamp-$n"; $n++ }
New-Item -ItemType Directory -Force -Path $candidate | Out-Null
$ReportDir = $candidate

$argErrors = @()
if ($args.Count) { $argErrors += "unknown arguments: $($args -join ' ')" }
$parsed = 0
if ([int]::TryParse($MinTests, [ref]$parsed) -and $parsed -ge 1) { $MinTestsN = $parsed } else { $argErrors += "invalid -MinTests '$MinTests'" }
if ([int]::TryParse($MinSmokeChecks, [ref]$parsed) -and $parsed -ge 1) { $MinSmokeN = $parsed } else { $argErrors += "invalid -MinSmokeChecks '$MinSmokeChecks'" }
if ($argErrors.Count) {
    $gates.arguments = New-Gate 'UNVERIFIED' $null ($argErrors -join '; ') $null
    Complete-Run
}

# Child processes must not inherit mock settings or write non-UTF-8 output.
$savedEnv = @{}
foreach ($name in @('LLM_MOCK_DELAY_MS', 'LLM_MOCK_ERROR_RATE', 'LLM_MOCK_SEED', 'PYTHONIOENCODING')) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
$env:PYTHONIOENCODING = 'utf-8'

try {
    # ---- 1. Environment record + preflight ---------------------------------------------------
    $envLines = @(
        "timestamp: $(Get-Date -Format o)",
        "project_root: $ProjectRoot",
        "python: $Python",
        "powershell: $($PSVersionTable.PSVersion)"
    )
    try {
        $os = Get-CimInstance Win32_OperatingSystem
        $envLines += "os: $($os.Caption) $($os.Version)"
    } catch { $envLines += "os: unavailable ($($_.Exception.Message))" }
    # Native stderr becomes an error record on PS 5.1, so relax 'Stop' for this call only.
    $ErrorActionPreference = 'Continue'
    try {
        $gitOut = & git -C $ProjectRoot status --short 2>$null
        if ($LASTEXITCODE -eq 0) { $envLines += "git: repository"; $envLines += $gitOut }
        else { $envLines += "git: not a git repository (exit $LASTEXITCODE)" }
    } catch { $envLines += "git: not available" }
    finally { $ErrorActionPreference = 'Stop' }

    $preLog = Join-Path $ReportDir 'preflight.log'
    if (-not (Test-Path $Python)) {
        $gates.preflight = New-Gate 'UNVERIFIED' $null "Python interpreter not found: $Python" $preLog
        Write-Text $preLog "Python interpreter not found: $Python"
    } else {
        $code = Invoke-Logged $Python @('-c', 'import sys, fastapi, uvicorn, pytest, httpx, pydantic; print(sys.version); print(\"fastapi\", fastapi.__version__, \"pydantic\", pydantic.__version__)') $preLog
        if ($code -eq 0) {
            $gates.preflight = New-Gate 'PASS' 0 'interpreter starts and required packages import' $preLog
            Invoke-Logged $Python @('-m', 'pip', 'freeze') (Join-Path $ReportDir 'pip-freeze.txt') | Out-Null
        } elseif ($null -eq $code) {
            $gates.preflight = New-Gate 'UNVERIFIED' $null "Python could not be launched (permission/environment): $($script:LaunchError)" $preLog
        } else {
            $gates.preflight = New-Gate 'UNVERIFIED' $code "Python launched but exited $code before importing required packages (environment problem, not a project test failure)" $preLog
        }
    }
    Write-Text (Join-Path $ReportDir 'environment.txt') (($envLines + "preflight: $($gates.preflight.status) - $($gates.preflight.reason)") -join "`r`n")
    $ready = $gates.preflight.status -eq 'PASS'

    # ---- 2. pytest ------------------------------------------------------------------------------
    $pyLog = Join-Path $ReportDir 'pytest.log'
    if ($SkipPytest) {
        $gates.pytest = New-Gate 'UNVERIFIED' $null 'skipped by -SkipPytest (not executed)' $null
    } elseif (-not $ready) {
        $gates.pytest = New-Gate 'UNVERIFIED' $null 'not executed: preflight did not pass' $null
    } else {
        $junit = Join-Path $ReportDir 'pytest-junit.xml'
        $code = Invoke-Logged $Python @('-m', 'pytest', '-p', 'no:cacheprovider', '-q', '-rfE', "--junitxml=$junit") $pyLog

        # Step 1: record the exit-code verdict immediately, before touching the JUnit file,
        # so a failure is never lost if the evidence turns out to be unreadable.
        switch ($code) {
            0 { $gates.pytest = New-Gate 'UNVERIFIED' 0 'exit 0 but JUnit evidence not yet confirmed' $pyLog }
            1 { $gates.pytest = New-Gate 'FAIL' 1 'test failures' $pyLog }
            2 { $gates.pytest = New-Gate 'FAIL' 2 'interrupted or collection error' $pyLog }
            5 { $gates.pytest = New-Gate 'FAIL' 5 'no tests were collected/executed' $pyLog }
            { $_ -in 3, 4 } { $gates.pytest = New-Gate 'UNVERIFIED' $code 'pytest internal or usage error (tool problem)' $pyLog }
            $null { $gates.pytest = New-Gate 'UNVERIFIED' $null "pytest could not be launched: $($script:LaunchError)" $pyLog }
            default { $gates.pytest = New-Gate 'UNVERIFIED' $code 'unexpected pytest exit code' $pyLog }
        }

        # Step 2: read the JUnit evidence. Problems with it are a separate UNVERIFIED gate and
        # never replace the FAIL recorded above; exit 0 is upgraded to PASS only with valid evidence.
        if ($null -ne $code) {
            $c = $null; $evidenceError = $null
            if (-not (Test-Path $junit)) { $evidenceError = 'JUnit report missing' }
            else {
                try {
                    $c = @{ tests = 0; failures = 0; errors = 0; skipped = 0 }
                    foreach ($s in ([xml](Get-Content $junit -Raw)).SelectNodes('//testsuite')) {
                        foreach ($k in @('tests', 'failures', 'errors', 'skipped')) { $c[$k] += [int]$s.GetAttribute($k) }
                    }
                    $c.executed = $c.tests - $c.skipped
                } catch { $c = $null; $evidenceError = "JUnit report unreadable: $($_.Exception.Message)" }
            }
            if ($evidenceError) {
                $gates.pytest_evidence = New-Gate 'UNVERIFIED' $null $evidenceError $junit
                $gates.pytest.reason += "; $evidenceError"
            } else {
                $counts = "executed=$($c.executed) failures=$($c.failures) errors=$($c.errors) skipped=$($c.skipped)"
                if ($code -ne 0) { $gates.pytest.reason += ": $counts" }
                elseif ($c.failures + $c.errors -gt 0) { $gates.pytest = New-Gate 'FAIL' 0 "inconsistent result: $counts" $pyLog }
                elseif ($c.executed -lt $MinTestsN) { $gates.pytest = New-Gate 'FAIL' 0 "required tests missing: $counts, minimum $MinTestsN" $pyLog }
                else { $gates.pytest = New-Gate 'PASS' 0 $counts $pyLog }
            }
        }
    }

    # ---- 3. TCP smoke test ----------------------------------------------------------------------
    $smLog = Join-Path $ReportDir 'smoke.log'
    if ($SkipSmoke) {
        $gates.smoke = New-Gate 'UNVERIFIED' $null 'skipped by -SkipSmoke (not executed)' $null
    } elseif (-not $ready) {
        $gates.smoke = New-Gate 'UNVERIFIED' $null 'not executed: preflight did not pass' $null
    } else {
        $smokeJson = Join-Path $ReportDir 'smoke.json'
        $code = Invoke-Logged $Python @('-B', (Join-Path $ProjectRoot 'scripts\smoke_tcp.py'), '--project-root', $ProjectRoot,
            '--log-dir', (Join-Path $ReportDir 'smoke-servers'), '--json-out', $smokeJson) $smLog

        # The exit code alone is not trusted: cross-check it with the evidence file.
        $sj = $null; $evidence = $null
        if (-not (Test-Path $smokeJson)) { $evidence = 'smoke.json missing' }
        else { try { $sj = Get-Content $smokeJson -Raw | ConvertFrom-Json } catch { $evidence = 'smoke.json unreadable' } }
        $checks = @(if ($sj) { $sj.checks })
        $failed = @($checks | Where-Object { $_.status -eq 'FAIL' } | ForEach-Object { $_.name })
        $notPassed = @($checks | Where-Object { $_.status -ne 'PASS' } | ForEach-Object { "$($_.name)=$($_.status)" })
        $detail = "$($checks.Count - $notPassed.Count)/$($checks.Count) checks passed"
        if ($notPassed.Count) { $detail += "; not passed: " + ($notPassed -join ', ') }
        if ($sj -and $sj.reason) { $detail += "; $($sj.reason)" }
        if ($evidence) { $detail = $evidence }

        if ($null -eq $code) { $gates.smoke = New-Gate 'UNVERIFIED' $null "smoke test could not be launched: $($script:LaunchError)" $smLog }
        elseif ($failed.Count) { $gates.smoke = New-Gate 'FAIL' $code "failed checks recorded (exit $code); $detail" $smLog }
        elseif ($code -eq 1) { $gates.smoke = New-Gate 'FAIL' 1 $detail $smLog }
        elseif ($code -eq 2) { $gates.smoke = New-Gate 'UNVERIFIED' 2 "environment prevented TCP checks; $detail" $smLog }
        elseif ($code -ne 0) { $gates.smoke = New-Gate 'FAIL' $code "smoke script crashed (exit $code); $detail" $smLog }
        elseif ($evidence) { $gates.smoke = New-Gate 'UNVERIFIED' 0 "exit 0 but no usable result evidence ($evidence)" $smLog }
        elseif ($sj.verdict -ne 'PASS') { $gates.smoke = New-Gate 'FAIL' 0 "inconsistent: exit 0 but smoke.json verdict '$($sj.verdict)'; $detail" $smLog }
        elseif ($checks.Count -lt $MinSmokeN) { $gates.smoke = New-Gate 'FAIL' 0 "required smoke checks missing: $($checks.Count) recorded, minimum $MinSmokeN" $smLog }
        elseif ($notPassed.Count) { $gates.smoke = New-Gate 'FAIL' 0 "inconsistent: exit 0 but $detail" $smLog }
        else { $gates.smoke = New-Gate 'PASS' 0 $detail $smLog }
    }
} finally {
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
}

# ---- 4. Verdict + report (FAIL > UNVERIFIED > PASS; PASS only with a complete report) ----------
Complete-Run
