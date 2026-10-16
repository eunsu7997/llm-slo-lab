<#
.SYNOPSIS
    Self-test of the quality gate (verify.ps1 + smoke_tcp.py). Injects failures into TEMPORARY copies
    of the project (including copies of the scripts themselves) and checks that the reported verdict,
    exit code and summary.json are what docs/quality-gates.md requires.
    The original project is not modified; the existing .venv is only used read-only as the interpreter.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\selftest_verify.ps1
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ReportRoot = Join-Path $ProjectRoot "reports\selftest-$stamp"
$TempRoot = Join-Path $env:TEMP "llm-slo-lab-selftest-$stamp"
New-Item -ItemType Directory -Force -Path $ReportRoot, $TempRoot | Out-Null
$Utf8 = New-Object System.Text.UTF8Encoding $false

function New-Copy([string]$Name) {
    $dst = Join-Path $TempRoot $Name
    New-Item -ItemType Directory -Path $dst | Out-Null
    foreach ($item in @('app', 'perf', 'tests', 'scripts', 'pytest.ini')) {
        Copy-Item -Recurse -Path (Join-Path $ProjectRoot $item) -Destination $dst
    }
    Get-ChildItem -Recurse -Directory -Filter '__pycache__' $dst | Remove-Item -Recurse -Force
    return $dst
}

function Edit-Copy([string]$File, [string]$Anchor, [string]$Replacement) {
    # Fault injection by exact text replacement in the COPY; fails loudly if the anchor moved.
    $text = [System.IO.File]::ReadAllText($File)
    if (-not $text.Contains($Anchor)) { throw "injection anchor not found in $File : $Anchor" }
    [System.IO.File]::WriteAllText($File, $text.Replace($Anchor, $Replacement), $Utf8)
}

function Set-FakeSmoke([string]$Dir, [string]$Body) {
    $py = "import json, sys`nout = sys.argv[sys.argv.index('--json-out') + 1]`n$Body`nsys.exit(0)`n"
    [System.IO.File]::WriteAllText((Join-Path $Dir 'scripts\smoke_tcp.py'), $py, $Utf8)
}

$failingTest = "def test_injected_failure():`r`n    assert False, 'injected'"

# Overwrites the JUnit report with truncated XML right after pytest finishes (in the COPY's verify.ps1).
function Set-CorruptJunit([string]$Dir) {
    Edit-Copy (Join-Path $Dir 'scripts\verify.ps1') '"--junitxml=$junit") $pyLog' '"--junitxml=$junit") $pyLog; Write-Text $junit ''<testsuites><testsuite tests="9'''
}
function Test-PytestExit($Summary, [int]$Expected) {
    if ($Summary.gates.pytest.exit_code -ne $Expected) { "pytest exit_code=$($Summary.gates.pytest.exit_code), injection needs $Expected" }
}

# Each case: expected exit, expected gate statuses, optional extra checks on the summary.
$cases = @(
    # ---- original 8 ----
    @{ name = 'baseline'; expect = 0; gates = @{ preflight = 'PASS'; pytest = 'PASS'; smoke = 'PASS' }; extra = @() },
    @{ name = 'pytest-failure'; expect = 1; gates = @{ pytest = 'FAIL' }; extra = @('-SkipSmoke')
       prepare = { param($d) Set-Content -Encoding ASCII (Join-Path $d 'tests\test_injected_failure.py') $failingTest } },
    @{ name = 'server-start-failure'; expect = 1; gates = @{ pytest = 'UNVERIFIED'; smoke = 'FAIL' }; extra = @('-SkipPytest')
       prepare = { param($d) Add-Content -Encoding ASCII (Join-Path $d 'app\main.py') "`r`nraise RuntimeError('injected startup failure')" } },
    @{ name = 'no-tests-collected'; expect = 1; gates = @{ pytest = 'FAIL' }; extra = @('-SkipSmoke')
       prepare = { param($d) Get-ChildItem (Join-Path $d 'tests') -Filter 'test_*.py' | Remove-Item } },
    @{ name = 'too-few-tests'; expect = 1; gates = @{ pytest = 'FAIL' }; extra = @('-SkipSmoke')
       prepare = { param($d) Remove-Item (Join-Path $d 'tests\test_contract.py') } },
    @{ name = 'python-missing'; expect = 2; gates = @{ preflight = 'UNVERIFIED'; pytest = 'UNVERIFIED'; smoke = 'UNVERIFIED' }
       python = 'MISSING'; reason = @{ preflight = 'not found' } },
    # Launcher starts, then exits 103 because pyvenv.cfg points to a missing base Python
    # (the error seen in the Codex sandbox). This is the "process ran, exit code != 0" branch.
    @{ name = 'python-launcher-exit-103'; expect = 2; gates = @{ preflight = 'UNVERIFIED'; pytest = 'UNVERIFIED'; smoke = 'UNVERIFIED' }
       python = 'BROKEN_HOME'; reason = @{ preflight = 'exited 103' } },
    @{ name = 'nothing-executed'; expect = 2; gates = @{ pytest = 'UNVERIFIED'; smoke = 'UNVERIFIED' }; extra = @('-SkipPytest', '-SkipSmoke') },

    # ---- A. pytest FAIL, then an exception while producing the report -> still FAIL ----
    @{ name = 'A-pytest-fail-then-report-exception'; expect = 1; gates = @{ pytest = 'FAIL'; harness = 'UNVERIFIED' }; extra = @('-SkipSmoke')
       prepare = { param($d)
           Set-Content -Encoding ASCII (Join-Path $d 'tests\test_injected_failure.py') $failingTest
           Edit-Copy (Join-Path $d 'scripts\verify.ps1') '# ---- 4. Verdict' "throw 'injected report-generation failure'`r`n# ---- 4. Verdict" } },

    # ---- B. smoke check FAIL, then an environment error inside smoke -> still FAIL ----
    @{ name = 'B-smoke-fail-then-env-error'; expect = 1; gates = @{ pytest = 'UNVERIFIED'; smoke = 'FAIL' }; extra = @('-SkipPytest')
       prepare = { param($d)
           Edit-Copy (Join-Path $d 'scripts\smoke_tcp.py') "        smoke.concurrency()`n" "        smoke.check('injected check failure', False)`n        raise Unverified('injected environment error')`n" }
       check = { param($s, $dir)
           $sj = Get-Content (Join-Path $dir 'smoke.json') -Raw | ConvertFrom-Json
           $p = @()
           if ($sj.verdict -ne 'FAIL') { $p += "smoke.json verdict=$($sj.verdict), expected FAIL" }
           if (-not ($sj.checks | Where-Object { $_.name -eq 'injected check failure' -and $_.status -eq 'FAIL' })) { $p += 'injected FAIL check not preserved' }
           if (-not ($sj.checks | Where-Object { $_.status -eq 'UNVERIFIED' })) { $p += 'environment error not recorded' }
           if ($s.gates.smoke.exit_code -ne 1) { $p += "smoke process exit $($s.gates.smoke.exit_code), expected 1" }
           $p } },

    # ---- C. summary.md cannot be finalised (summary.json.tmp already written) -> never PASS ----
    @{ name = 'C1-summary-md-write-failure'; expect = 2; gates = @{ pytest = 'PASS'; smoke = 'PASS'; report = 'UNVERIFIED' }
       prepare = { param($d) Edit-Copy (Join-Path $d 'scripts\verify.ps1') 'Move-Item -Force "$mdPath.tmp" $mdPath' "throw 'injected summary.md write failure'" }
       check = { param($s, $dir)
           $p = @()
           if ($s.complete -ne $false) { $p += 'summary.json not marked complete=false' }
           if (Test-Path (Join-Path $dir 'summary.md')) { $p += 'summary.md exists although its write failed' }
           if (Get-ChildItem $dir -Filter '*.tmp') { $p += 'temporary summary files left behind' }
           $p } },
    # ---- C2. summary.md saved, final summary.json move fails -> never PASS ----
    @{ name = 'C2-summary-json-finalize-failure'; expect = 2; gates = @{ pytest = 'PASS'; smoke = 'PASS'; report = 'UNVERIFIED' }
       prepare = { param($d) Edit-Copy (Join-Path $d 'scripts\verify.ps1') 'Move-Item -Force "$jsonPath.tmp" $jsonPath' "throw 'injected summary.json finalize failure'" }
       check = { param($s, $dir) if ($s.complete -ne $false) { 'summary.json not marked complete=false' } } },

    # ---- D. smoke exit 0 but its JSON records a failed check -> FAIL ----
    @{ name = 'D-smoke-exit0-with-failed-check'; expect = 1; gates = @{ pytest = 'PASS'; smoke = 'FAIL' }
       prepare = { param($d) Set-FakeSmoke $d "json.dump({'verdict': 'PASS', 'reason': '', 'checks': [{'name': 'x', 'status': 'FAIL', 'detail': 'injected'}]}, open(out, 'w'))" } },
    # ---- E1. smoke exit 0 but no JSON evidence -> UNVERIFIED (no evidence, not a confirmed defect) ----
    @{ name = 'E1-smoke-exit0-no-json'; expect = 2; gates = @{ pytest = 'PASS'; smoke = 'UNVERIFIED' }
       prepare = { param($d) Set-FakeSmoke $d "pass" }; reason = @{ smoke = 'smoke.json missing' } },
    # ---- E2. smoke exit 0, JSON says PASS but contains no checks -> FAIL (required checks missing) ----
    @{ name = 'E2-smoke-exit0-empty-checks'; expect = 1; gates = @{ pytest = 'PASS'; smoke = 'FAIL' }
       prepare = { param($d) Set-FakeSmoke $d "json.dump({'verdict': 'PASS', 'reason': '', 'checks': []}, open(out, 'w'))" }
       reason = @{ smoke = 'required smoke checks missing' } },

    # ---- F. Start-Process itself throws (execute permission denied) -> UNVERIFIED, launch-error branch ----
    @{ name = 'F-python-access-denied'; expect = 2; gates = @{ preflight = 'UNVERIFIED'; pytest = 'UNVERIFIED'; smoke = 'UNVERIFIED' }
       python = 'ACCESS_DENIED'; reason = @{ preflight = 'could not be launched' }
       check = { param($s, $dir) if ($null -ne $s.gates.preflight.exit_code) { "preflight exit_code=$($s.gates.preflight.exit_code); expected none (process must not start)" } } },

    # ---- G. bad / unknown command-line arguments -> UNVERIFIED (2), not FAIL (1) ----
    @{ name = 'G-invalid-arguments'; expect = 2; gates = @{ arguments = 'UNVERIFIED' }; extra = @('-MinTests', 'abc', '-Bogus', 'x') },

    # ---- H. pytest exit code + corrupted JUnit XML: failure is kept, bad evidence is a separate UNVERIFIED ----
    @{ name = 'H1-pytest-exit1-corrupt-junit'; expect = 1; gates = @{ pytest = 'FAIL'; pytest_evidence = 'UNVERIFIED' }; extra = @('-SkipSmoke')
       prepare = { param($d)
           Set-Content -Encoding ASCII (Join-Path $d 'tests\test_injected_failure.py') $failingTest
           Set-CorruptJunit $d }
       check = { param($s, $dir) Test-PytestExit $s 1 } },
    @{ name = 'H2-pytest-exit2-corrupt-junit'; expect = 1; gates = @{ pytest = 'FAIL'; pytest_evidence = 'UNVERIFIED' }; extra = @('-SkipSmoke')
       prepare = { param($d)
           Set-Content -Encoding ASCII (Join-Path $d 'tests\test_injected_syntax_error.py') "def broken(:`r`n    pass"
           Set-CorruptJunit $d }
       check = { param($s, $dir) Test-PytestExit $s 2 } },
    @{ name = 'H5-pytest-exit5-corrupt-junit'; expect = 1; gates = @{ pytest = 'FAIL'; pytest_evidence = 'UNVERIFIED' }; extra = @('-SkipSmoke')
       prepare = { param($d)
           Get-ChildItem (Join-Path $d 'tests') -Filter 'test_*.py' | Remove-Item
           Set-CorruptJunit $d }
       check = { param($s, $dir) Test-PytestExit $s 5 } },
    # exit 0 but evidence unreadable: everything else passes, so the run must end UNVERIFIED, never PASS.
    @{ name = 'H0-pytest-exit0-corrupt-junit'; expect = 2; gates = @{ pytest = 'UNVERIFIED'; pytest_evidence = 'UNVERIFIED'; smoke = 'PASS' }
       prepare = { param($d) Set-CorruptJunit $d }
       check = { param($s, $dir) Test-PytestExit $s 0 } }
)

function Get-PythonFor($Case, [string]$Dir) {
    switch ($Case.python) {
        'MISSING' { return (Join-Path $Dir 'missing\python.exe') }
        'BROKEN_HOME' {
            $v = Join-Path $Dir 'brokenvenv'
            New-Item -ItemType Directory -Path (Join-Path $v 'Scripts') | Out-Null
            Copy-Item $Python (Join-Path $v 'Scripts\python.exe')
            Set-Content -Encoding ASCII (Join-Path $v 'pyvenv.cfg') "home = $(Join-Path $Dir 'no-such-python')`r`ninclude-system-site-packages = false"
            return (Join-Path $v 'Scripts\python.exe')
        }
        'ACCESS_DENIED' {
            $v = Join-Path $Dir 'deniedvenv'
            New-Item -ItemType Directory -Path (Join-Path $v 'Scripts') | Out-Null
            $exe = Join-Path $v 'Scripts\python.exe'
            Copy-Item $Python $exe
            $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            & icacls.exe $exe /deny "${me}:(X)" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls could not deny execute permission (exit $LASTEXITCODE)" }
            return $exe
        }
        default { return $Python }
    }
}

$rows = @()
$allOk = $true
try {
    foreach ($c in $cases) {
        $problems = @()
        $code = $null
        $caseReports = Join-Path $ReportRoot $c.name
        Write-Host "=== $($c.name) ==="
        try {
            $dir = New-Copy $c.name
            if ($c.prepare) { & $c.prepare $dir }
            $py = Get-PythonFor $c $dir
            $verifyArgs = @('-ProjectRoot', $dir, '-ReportRoot', $caseReports, '-Python', $py) + $c.extra
            # The COPY's verify.ps1 is run, so script-level injections apply; un-patched copies are identical.
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir 'scripts\verify.ps1') @verifyArgs | Out-Host
            $code = $LASTEXITCODE
        } catch {
            $problems += "case setup failed: $($_.Exception.Message)"
        }

        if ($null -ne $code) {
            if ($code -ne $c.expect) { $problems += "exit $code, expected $($c.expect)" }
            $summaryFile = Get-ChildItem -Recurse -Path $caseReports -Filter 'summary.json' -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $summaryFile) { $problems += 'summary.json missing' }
            else {
                $s = Get-Content $summaryFile.FullName -Raw | ConvertFrom-Json
                $verdictFor = @{ 0 = 'PASS'; 1 = 'FAIL'; 2 = 'UNVERIFIED' }
                if ($s.exit_code -ne $code) { $problems += "summary exit_code $($s.exit_code) != process exit $code" }
                if ($s.verdict -ne $verdictFor[[int]$code]) { $problems += "summary verdict $($s.verdict) != exit $code" }
                if ($s.verdict -eq 'PASS' -and $s.complete -ne $true) { $problems += 'PASS recorded in an incomplete report' }
                foreach ($g in $c.gates.Keys) {
                    $actual = $s.gates.$g.status
                    if ($actual -ne $c.gates[$g]) { $problems += "$g=$actual, expected $($c.gates[$g])" }
                }
                if ($c.reason) {
                    foreach ($g in $c.reason.Keys) {
                        if ($s.gates.$g.reason -notmatch [regex]::Escape($c.reason[$g])) { $problems += "$g reason '$($s.gates.$g.reason)' lacks '$($c.reason[$g])'" }
                    }
                }
                if ($c.check) { $problems += @(& $c.check $s $summaryFile.DirectoryName) }
            }
        }
        $problems = @($problems | Where-Object { $_ })
        $ok = $problems.Count -eq 0
        if (-not $ok) { $allOk = $false }
        $rows += [pscustomobject]@{ case = $c.name; expected_exit = $c.expect; actual_exit = $code
                                    result = $(if ($ok) { 'OK' } else { 'MISMATCH' }); notes = ($problems -join '; ') }
    }
} finally {
    Get-ChildItem -Recurse -Filter 'python.exe' $TempRoot -ErrorAction SilentlyContinue | ForEach-Object { & icacls.exe $_.FullName /reset | Out-Null }
    Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
}

$table = $rows | Format-Table -AutoSize -Wrap | Out-String -Width 220
$table | Out-Host
[System.IO.File]::WriteAllText((Join-Path $ReportRoot 'selftest-summary.txt'), $table, $Utf8)
$rows | ConvertTo-Json | ForEach-Object { [System.IO.File]::WriteAllText((Join-Path $ReportRoot 'selftest-summary.json'), $_, $Utf8) }
if ($allOk) { Write-Host "SELFTEST OK: all $($rows.Count) cases behaved as expected"; exit 0 }
Write-Host "SELFTEST FAILED: $(@($rows | Where-Object { $_.result -ne 'OK' }).Count) case(s) mismatched"; exit 1
