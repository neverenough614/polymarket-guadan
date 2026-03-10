param(
    # 回测时间窗口（北京时间，UTC+8）。支持格式：
    # 1) 2026-02-01 08:00:00
    # 2) 2026-02-01T08:00:00
    [string]$StartBjt = "2026-02-01 08:00:00",
    [string]$EndBjt = "2026-03-01 08:00:00",

    [string]$StrategyJson = "poly_maker_rs/strategy_tokens.json",
    [string]$DatasetCsv = "backtest/data/backtest_dataset.csv",
    [string]$OptimizedJson = "backtest/optimized_params.json",
    [string]$CandidatesCsv = "backtest/data/optimization_candidates.csv",
    [string]$BaselineCsv = "backtest/data/baseline_metrics.csv",
    [string]$OptimizedCsv = "backtest/data/optimized_metrics.csv"
)

$ErrorActionPreference = "Stop"

function Convert-BjtToUtcIso {
    param([string]$BjtText)

    $formats = @(
        "yyyy-MM-dd HH:mm:ss",
        "yyyy-MM-ddTHH:mm:ss",
        "yyyy-MM-dd HH:mm",
        "yyyy-MM-ddTHH:mm"
    )

    $parsed = [datetime]::MinValue
    $ok = $false
    foreach ($fmt in $formats) {
        if ([datetime]::TryParseExact(
            $BjtText,
            $fmt,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::None,
            [ref]$parsed
        )) {
            $ok = $true
            break
        }
    }

    if (-not $ok) {
        throw "无法解析北京时间: $BjtText。请用 'yyyy-MM-dd HH:mm:ss' 或 'yyyy-MM-ddTHH:mm:ss'"
    }

    $offset = New-Object System.TimeSpan(8, 0, 0)
    $dto = New-Object System.DateTimeOffset($parsed, $offset)
    return $dto.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Invoke-Step {
    param(
        [string]$Title,
        [scriptblock]$Action
    )
    Write-Host "=== $Title ===" -ForegroundColor Cyan
    & $Action
    Write-Host ""
}

$StartUtc = Convert-BjtToUtcIso -BjtText $StartBjt
$EndUtc = Convert-BjtToUtcIso -BjtText $EndBjt

Write-Host "Polymarket backtest pipeline start" -ForegroundColor Green
Write-Host ("BJT window: " + $StartBjt + " -> " + $EndBjt)
Write-Host ("UTC window: " + $StartUtc + " -> " + $EndUtc)
Write-Host ""

New-Item -ItemType Directory -Force -Path "backtest/data" | Out-Null

Invoke-Step -Title "[1/5] collect history data" -Action {
    python "backtest/data_collector.py" `
      --strategy-json "$StrategyJson" `
      --output-csv "$DatasetCsv" `
      --start "$StartUtc" `
      --end "$EndUtc"
}

Invoke-Step -Title "[2/5] optimize parameters" -Action {
    python "backtest/optimize_params.py" `
      --dataset-csv "$DatasetCsv" `
      --out-json "$OptimizedJson" `
      --out-candidates-csv "$CandidatesCsv"
}

Invoke-Step -Title "[3/5] baseline backtest" -Action {
    python "backtest/run_backtest.py" `
      --dataset-csv "$DatasetCsv" `
      --out-csv "$BaselineCsv"
}

Invoke-Step -Title "[4/5] optimized backtest" -Action {
    python "backtest/run_backtest.py" `
      --dataset-csv "$DatasetCsv" `
      --params-json "$OptimizedJson" `
      --out-csv "$OptimizedCsv"
}

Invoke-Step -Title "[5/5] compare report" -Action {
    python "analyze_lp_performance.py" `
      --baseline-backtest-csv "$BaselineCsv" `
      --optimized-backtest-csv "$OptimizedCsv" `
      --candidates-csv "$CandidatesCsv"
}

Write-Host "Done." -ForegroundColor Green
Write-Host ("optimized params: " + $OptimizedJson)
Write-Host ("candidate table: " + $CandidatesCsv)
Write-Host ("baseline metrics: " + $BaselineCsv)
Write-Host ("optimized metrics: " + $OptimizedCsv)

