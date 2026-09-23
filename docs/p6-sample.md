# P6 JEV sample runner

`jevquant p6-sample` runs the plan's one-session or 20-session paper-account chain on the supplied Moutai history. It calls JEV after each completed five-minute bar, and models an order only on the exact bar where its five-minute delay expires. Pending orders suppress duplicate decisions until resolved. The account starts with CNY 1,000,000, targets 80% for a new position, applies T+1, price protection, dated fees, and prior-session same-slot liquidity limits. It never connects to a broker.

Before a request leaves the machine, the provider state uses `ASSET_001`, 24 completed five-minute bars normalized to a rolling price anchor, relative same-slot volume, causal daily features and account ratios. It excludes the ticker, calendar date, absolute market prices, exact share count and absolute account cash. Local audit logs retain the actual dates, orders and fills under the ignored `artifacts/` directory; they must not be committed.

The supplied Parquet labels do not prove interval semantics. The runner therefore requires an explicit acknowledgment that it is using the retrospective hypothesis `label L = [L-5 minutes, L]`, with availability at interval end. Status files are also retrospective. The resulting reports are marked diagnostic, not formal historical execution evidence or forward simulation.

Example, after setting the following PowerShell variables to the local data and status files:

```powershell
$env:PYTHONPATH = "src"
python -m jevquant.cli p6-sample `
  --minute-root $minuteRoot `
  --daily-csv $dailyCsv `
  --status-2022-2023 $status2023 `
  --status-2024-plus $status2024 `
  --start 2023-01-03 `
  --sessions 1 `
  --output artifacts/p6/one-session `
  --acknowledge-historical-data-to-live-jev
```

Use `--sessions 20` for the 20-session phase. Each completed session writes a fingerprinted `checkpoint.json`. To resume the same run after interruption, pass the same arguments and `--resume`. The runner restores the account by replaying fill and company-action events, verifies cash, shares, receivables and NAV against the checkpoint, removes records from any incomplete session, then reuses validated JEV responses.

Each output directory contains `decisions.jsonl`, `orders.jsonl`, `fills.jsonl`, `nav.jsonl`, `jev-usage.jsonl`, the validated response cache, the checkpoint and `summary.json`. A provider error is recorded as a skipped decision with no order and does not mutate the account. A successful later retry resolves the earlier error for API-gap reporting while preserving the original error record. Any unresolved API gap remains explicit in the summary.

The completed 20-session retrospective diagnostic (2023-01-03 through 2023-02-06) contains 961 snapshots: JEV returned BUY 3 times and WAIT 954 times; 3 intervening bars skipped duplicate calls while an order was pending. All 3 buy orders were rejected because the adverse-slippage proxy price lay outside that execution bar's OHLC range, so there were no fills and the CNY 1,000,000 account remained in cash. This shows JEV did propose buys; the simulation did not execute them. It is a diagnostic sample, not evidence of profitability or forward performance.
