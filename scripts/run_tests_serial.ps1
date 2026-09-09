$tests = Get-ChildItem "$PSScriptRoot\..\tests\test_*.py" | Select-Object -ExpandProperty Name
foreach ($f in $tests) {
    Write-Host "=== $f ===" -NoNewline
    $out = & python -m pytest "tests/$f" -q --tb=line 2>&1 | Select-Object -Last 2
    Write-Host " $out"
}
