# Implementation status

Updated: 2026-09-23

## Complete in this checkout

- P0: read-only local inventory for Moutai daily and five-minute files; added 154-row BaoStock raw supplement inventory (2026-01-05 to 2026-08-21) with its late capture time recorded.
- P1/P2: Python 3.11 environment, locked dependencies, CLI, basic account, dated fee schedule, T+1 lots, order reservations, execution proxy, SQLite event log, synthetic examples.
- P5: pinned SDK adapter contract, strict response validation, identical-request cache and usage receipts. One real connectivity/schema request using only synthetic state resolved to `jev-1.13.0` and passed validation.
- Synthetic integration: two-session Mock decision → order → fill → T+1 → cash/NAV; 34 tests pass.

## Not accepted yet

- P0 data gates remain open: five-minute time-label meaning, special 09:30/15:00 rows, full daily cross-check, status/calendar/rule tables and corporate actions.
- The existing A-share `quant_v2` integration has a configured private `.env`. JevQuant references that file locally through its ignored `.env`; it reads the key in memory without copying or printing it. The live smoke call used synthetic state only and did not place an order.
- P6's planned real one-day and 20-trading-day runs have not run. The two-session Mock flow is only an engineering test, not strategy evidence.
- No historical strategy comparison, forward simulation, or profitability claim has been made.

## API usage policy

There is no request-count or spend hard cap, and no automatic shutdown based on cost. Each real response records requested/resolved model, input/output tokens and estimated cost when returned by the provider. Repeating an identical validated request reuses the local cache. Cache and usage records are local run artifacts and are Git-ignored.

For an existing private key file, set `JEVQUANT_TYPESAFE_ENV_FILE` to its path in the local process environment. JevQuant reads only the `TYPESAFE_API_KEY` entry; it does not copy the file, print the key, or include the path in public project files.
