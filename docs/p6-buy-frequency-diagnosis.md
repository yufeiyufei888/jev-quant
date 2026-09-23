# P6 buy-signal frequency diagnosis

## Finding

The 20-session retrospective Moutai sample contains 957 completed JEV choices: 954 `WAIT` and 3 `BUY`. Three additional bar snapshots skipped a call while an order was pending. The account remained flat, so every model call had `BUY` and `WAIT` available; the account rules did not disable buying. `can_open_min_lot` was true for the tested account and price range.

The response probabilities show a mostly cautious distribution. Across `WAIT` responses, mean P(BUY) was 0.301; 28 were at least 0.45 and the maximum was 0.49. The three `BUY` responses had P(BUY) of 0.51, 0.51 and 0.53, with reported confidence of 0.01, 0.02 and 0.06. The parser selects `BUY` only when P(BUY) is strictly greater than P(WAIT); a tie selects `WAIT`. Confidence is recorded but is not itself a buy gate.

The separate `instant_snapshot_close` run completed the same 20 dates with 960 JEV calls, all `WAIT`; mean P(BUY) was 0.236, median 0.23 and maximum 0.48. There were no fills because JEV did not recommend a buy, not because the fill engine rejected one. On the first 192 matched snapshots, market and account states were identical between runs: mean P(BUY) was 0.274 with the original instruction/context and 0.217 with the immediate-fill instruction/context. Because both the wording and execution fields changed together and the model may be stochastic, this comparison does not isolate a single cause.

## Likely contributors

- The instruction explicitly says to choose `WAIT` when evidence is insufficient, but defines no measurable entry conditions that would establish sufficient evidence.
- The original choice description says `BUY` requests opening the position “later,” while the user expectation is immediate order submission. The original five-minute delay/slippage context may affect choices. However, simply changing to immediate-fill wording/context did not increase BUY frequency in this sample; it fell to zero.
- The decision is based on one anonymized security and a numerical snapshot, without ticker-specific context, news or a rule-based entry signal. This sample cannot establish how JEV behaves across a broad stock universe.

These are prompt and task-design explanations, not proof of the model's private reasoning. The response contract saves probabilities and confidence, not a rationale, so the logs cannot explain each individual `WAIT`.

## Ruled out by this sample

- No position or sellability rule blocked these buys: the account was flat and `BUY` was an allowed action.
- No confidence cutoff suppressed a buy: the parser does not threshold confidence.
- The three BUY recommendations were not suppressed at decision time. They became orders and were later rejected by the separate fill proxy because its adverse-slippage estimate fell outside the execution bar's OHLC.

## Next diagnostic

Use the same saved snapshots in a small, preregistered prompt comparison. Keep data, model, execution context and probability parser fixed; change only one wording/entry-criterion variable at a time. Compare BUY frequency, output confidence and recommendation stability. Do not select a prompt because it merely produces more buys; assess later-period performance and execution separately. This P6 sample is retrospective and too short to evaluate profitability.
