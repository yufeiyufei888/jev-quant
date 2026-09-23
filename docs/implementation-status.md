# Implementation status

Updated: 2026-09-23

## Complete in this checkout

- P0: read-only Moutai inventory plus a full five-minute audit of 1,032 date partitions. Every date has 49 rows on the same clock-label grid, with no missing Moutai rows, duplicate timestamps, row/file date mismatches across 50,568 rows, or structural invalids. Seven mixed-unit volume dates are normalized by source-hash-bound rules, yielding exact volume reconciliation on all 1,032 dates. For the planned 2023–2024 development window, 484 daily rows and 484 minute partitions are present and daily fields are complete. The 74 blank daily rows all predate 2013. Amounts exactly reconcile on only 22/1,032 days; max delta is CNY 191. Timestamp interval meaning, endpoint execution semantics, point-in-time status/rules and full corporate-action accounting remain open.
- P1/P2: Python 3.11 environment, locked dependencies, CLI, basic account, dated fee schedule, T+1 lots, order reservations, execution proxy, SQLite event log, synthetic examples.
- P2 execution refinement: order intent carries its frozen cap plus all prior same-slot reference dates/volumes, median and fraction; the reference validates its own calculation. Open-proxy matching refuses missing/oversized references and never shrinks the order using the execution bar's final volume. The Mock receipt records all 20 source volumes and cap. Actual-slot evidence remains blocked by unverified timestamp semantics.
- P3: official 2023–2024 Moutai cash-dividend events are in a sourced event table. A causal total-return feature builder and `features-sample` CLI have generated a 20-session sample for 2023-07-03 through 2023-07-28. The ledger snapshots entitlement on record date, books a receivable on ex-date and transfers it to cash on payment date, with idempotent replay. All four sourced events replayed for a 100-share holding to CNY 9,977.50; a separate reducer independently matched cash, shares and receivables, and detected injected CNY 1 cash, receivable and share errors. Close availability at 15:05 is explicitly a simulation assumption. The sample is a pipeline check, not a strategy result.
- P4 policy logic: fixed intent rules and a frozen configuration are in place for CASH, BH80, BH50, BH_MAX, MA5_20 and seeded RANDOM (seeds 0–99; entry/exit probability 5%). MA uses prior-session closes only. This does not yet produce a baseline account replay or comparative NAV report.
- P5: pinned SDK adapter contract, strict response validation, identical-request cache and usage receipts. One real connectivity/schema request using only synthetic state resolved to `jev-1.13.0` and passed validation.
- Synthetic integration: two-session Mock decision → order → fill → T+1 → cash/NAV; 51 tests pass.

## Not accepted yet

- P0 remains blocked for formal historical execution pending timestamp semantics, point-in-time status/calendar/rule tables and official corporate-action accounting. The 49-label clock grid is verified but does not prove interval meaning or that endpoints are executable. The 74 early-history daily missing rows do not overlap the planned 2023–2024 development window.
- The existing A-share `quant_v2` integration has a configured private `.env`. JevQuant references that file locally through its ignored `.env`; it reads the key in memory without copying or printing it. The live smoke call used synthetic state only and did not place an order.
- P6's planned real one-day and 20-trading-day runs have not run. The two-session Mock flow is only an engineering test, not strategy evidence.
- P4's 20-session and full development-window baseline account runs have not run. They require a shared, verified execution path; the available minute clock labels do not establish which complete bar follows a decision or is executable.
- No historical strategy comparison, forward simulation, or profitability claim has been made.

## API usage policy

There is no request-count or spend hard cap, and no automatic shutdown based on cost. Each real response records requested/resolved model, input/output tokens and estimated cost when returned by the provider. Repeating an identical validated request reuses the local cache. Cache and usage records are local run artifacts and are Git-ignored.

For an existing private key file, set `JEVQUANT_TYPESAFE_ENV_FILE` to its path in the local process environment. JevQuant reads only the `TYPESAFE_API_KEY` entry; it does not copy the file, print the key, or include the path in public project files.
