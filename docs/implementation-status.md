# Implementation status

Updated: 2026-09-23

## Complete in this checkout

- P0: read-only local inventory for Moutai daily and five-minute files; added 154-row BaoStock raw supplement inventory (2026-01-05 to 2026-08-21) with its late capture time recorded.
- P1/P2: Python 3.11 environment, locked dependencies, CLI, basic account, dated fee schedule, T+1 lots, order reservations, execution proxy, SQLite event log, synthetic examples.
- P5 offline: pinned SDK adapter contract, strict response validation, identical-request cache, usage receipt writer and Mock coverage.
- Synthetic integration: two-session Mock decision → order → fill → T+1 → cash/NAV; 31 tests pass.

## Not accepted yet

- P0 data gates remain open: five-minute time-label meaning, special 09:30/15:00 rows, full daily cross-check, status/calendar/rule tables and corporate actions.
- P5 live call has not run because `TYPESAFE_API_KEY` is not configured in this project environment.
- P6's planned real one-day and 20-trading-day runs have not run. The two-session Mock flow is only an engineering test, not strategy evidence.
- No historical strategy comparison, forward simulation, or profitability claim has been made.

## API usage policy

There is no request-count or spend hard cap, and no automatic shutdown based on cost. Each real response records requested/resolved model, input/output tokens and estimated cost when returned by the provider. Repeating an identical validated request reuses the local cache. Cache and usage records are local run artifacts and are Git-ignored.
