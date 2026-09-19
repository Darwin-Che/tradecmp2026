# Case 1: manual volatility trading workflow

Run `python case1_simple.py` from this directory. The program reads the simulator and prints instructions; you place and confirm every trade in the RIT client. Nothing is submitted automatically. Confirm the hardcoded account matches the client account.

The objective is to capture option mispricing relative to news-informed volatility while controlling stock-price exposure. There is no demonstrated profit-maximizing quantity or guaranteed profitable strategy here. This is a complete manual workflow for the selected straddle, not an optimizer across every possible option trade.

## What you trade

A straddle is one call plus one put with the same strike and expiry. LONG buys both; SHORT sells both. LONG benefits from volatility being greater than the price implies; SHORT expresses the opposite view. These are model-based comparisons, not forecasts of the direction of RTM.

When flat, the assistant selects the nearest common call/put strike. Ties use the narrower combined spread, then the lower strike. It keeps that strike until new volatility news. After entry, management uses the actual held tickers and displays `Held K`, independently of the next entry candidate.

Forecast ranges produce low, midpoint and high effective remaining volatility, using time-weighted variance. Actual announcements override forecasts. Unknown remaining weeks assume **25%** and are identified in the output. This assumption can materially drive the signal, especially before much news has arrived. The word “conservative” refers only to the available forecast bounds, not every possible volatility outcome.

The configured risk-free rate is **0%**. If the instructor changes it, change `RISK_FREE_RATE` before using the valuations. The current tick mapping follows the initial implementation and observed news (tick 150 announces week 3); the PDF's sample timetable is inconsistent. Do not change boundaries solely from that sample table.

## Entry and quantity

`LONG` requires at least **$10 estimated edge per straddle**, and `SHORT` at least **$15**, with the chosen edge greater than the opposite edge. `WAIT` means no new entry. Quotes and signals are snapshots, not promises of future execution prices.

Displayed entry edges deduct:

- Four option commissions for an assumed open-and-close cycle: $8 per straddle.
- Estimated initial stock hedge round-trip fees and stock spread.
- A $2 model buffer and an additional $2 rehedging allowance per straddle.

The rehedging allowance is an assumption, not an estimate fitted to practice data. Actual repeated hedging can cost more. Future option liquidation at model fair value is not assured. No percentage return on short premium is used.

Quantity is the largest integer allowed by the following constraints **when the account is flat**:

| Constraint | Default |
|---|---:|
| Desired ceiling (`ENTRY_QUANTITY`) | 100 straddles |
| Maximum option trade size | 100 contracts per leg |
| Option gross / absolute net limit | 2,500 / 1,000 contracts |
| RTM position capacity | 50,000 shares |
| Manual unhedged delta budget | 1,000 shares |
| Long premium plus entry commissions budget | $1,000 |

One straddle can approach 100 shares of unhedged delta; a single filled leg can too. The manual delta budget therefore caps a batch at **10 straddles**, with long batches often smaller because of premium. For calls and puts asking $1.14 each, one straddle costs $232 including entry commissions, so the default long batch is **4 straddles** ($928). Ten contracts per leg means twenty option contracts, not ten total.

This selects the largest *modeled* edge within the configured operational constraints because the estimated per-straddle edge is linear in quantity. It does **not** prove maximum realized profit. The 1,000-delta and $1,000 premium budgets are strategy choices, not case rules. Short positions have no equivalent premium-based maximum-loss bound; the delta budget is not a dollar-loss limit. Do not raise budgets merely because the program prints positive edge.

## Execute a complete cycle

1. **Verify flat positions and no pending client trades.** This assistant does not inspect pending trades. Refresh the latest entry signal and quotes; do not act on an old terminal block.
2. **Execute the displayed quantity in each of the two option legs.** BUY means pay the ask; SELL means sell at the bid. The case describes ample liquidity, but still verify actual fills. If price or signal changed while entering, review before placing more trades.
3. **Confirm positions in the client.** A partial-leg warning means only one leg or unequal quantities are visible. Do not add another straddle or blindly retry. Check for a rejected, pending, or already filled leg. The program shows current close instructions as one way to unwind an unintended partial position; it cannot know your pending orders.
4. **Follow management mode.** It persists while either options or stock remain open. A repeated entry block before fills is not permission to add another batch. The assistant never pyramids.
5. **Hedge when indicated.** `Required RTM trade +N` means buy N shares; `-N` means sell N. This is a change from your current stock position, not the target balance. Routine hedge recommendations begin at absolute delta 250. Below that, the target remains informational.
6. **Review the exit decision below.** If closing, sell long options at current bids or buy back short options at current asks. Confirm both closes actually fill. For partial closes, the next poll reflects only the remaining holdings.
7. **Flatten residual RTM after the options are gone.** `MANUAL FLATTEN` handles even small stock balances below the normal hedge threshold. Do not execute both an old hedge instruction and a new flatten instruction.
8. **Verify everything is zero.** Only then does the assistant resume looking for the next entry. If you return flat within a tick, entry review resumes on the next distinct tick.

## HOLD versus REVIEW EXIT

The program values *remaining* opportunity from the current executable closing price, without counting already-paid entry costs again:

- Long: `(conservative fair value − combined current bid) × 100`.
- Short: `(combined current ask − conservative fair value) × 100`.

It subtracts the $2 model buffer and $2 future rehedging allowance. If the result is **$5 or less per straddle**, it prints **REVIEW EXIT**. That threshold is a configurable strategy choice, not an empirically optimized parameter. A reversed forecast can trigger this even at a loss. Otherwise it prints **HOLD**, which means continue monitoring rather than ignore the position.

Partial/unbalanced positions and position-limit breaches also trigger review. If several pairs are held manually, one review reason prompts inspection of the entire portfolio and prints current closing instructions for all held options; this is not an optimized multi-position liquidation plan.

From tick **285**, new entries stop. From tick **290**, open options get an expiry exit review even if model advantage remains. This planned review is not a case requirement: the case cash-settles options at expiry and closes RTM at its last price. The assistant favors giving a human time to review and close. At expiry, unusable/zero quotes can prevent valuation; inspect the client and settlement instead of assuming the last quote is executable.

Exit review takes priority over a routine hedge; a delta-limit breach still prints an urgent warning. Decide whether you are closing or retaining the options before acting. If closing cannot happen promptly, actual remaining exposure still needs attention.

## Understand the dollar numbers

`Estimated total edge` is model edge multiplied by suggested quantity. It is neither cash received nor earned P&L.

`Close receive/pay` is signed liquidation cash flow at current bid/ask, before the separately displayed closing fee. Receiving $500 by selling an option does not imply $500 profit.

`Open-position mark-to-close P&L` uses current API `vwap` as the entry basis for each open option/stock row, marks long holdings to bid and shorts to ask, and subtracts prospective closing fees. It is unavailable if any needed basis or quote is missing. It **excludes entry fees, realized trades, prior hedge costs and fines**, so it is not overall strategy P&L. Use the RIT client for total account P&L and confirm its basis semantics. This implementation does not fabricate fill prices from the original recommendation.

## Rules and risk controls

The [case PDF](https://rotmanfrtl.github.io/RITCx-Volatility%20Trading%20Case.pdf), pages 3–4, states:

- RTM position limit 50,000 shares; maximum trade 10,000 shares.
- Options gross limit 2,500 contracts; net limit 1,000; maximum trade 100 contracts.
- Option multiplier 100, option commission $2/contract/side, stock fee $0.02/share/side.
- Delta must remain within ±7,000; excess incurs $0.10 per excess delta share per second.

The assistant shows position utilization, warns about estimated excess-delta fines, and caps displayed order chunks. A hedge target beyond stock capacity is blocked and calls for reducing options. Delta is model-estimated; these checks cannot guarantee that the simulator's risk estimate matches. Limits must be confirmed against the actual session configuration. Pending client orders are not included, so never have multiple unconfirmed entry batches.

## Practice, recording and validation

Record a practice round locally:

```bash
python case1_simple.py --record practice.jsonl
```

Replay the exact observed snapshots without connecting to the API:

```bash
python case1_simple.py --replay practice.jsonl
```

The log includes positions and market/news data but no credentials. It appends, and case resets clear assistant state during replay. Treat logs as account-sensitive. Replay checks the assistant's interpretation and transitions; it does **not** simulate fills, calculate counterfactual profits or prove an optimal parameter set.

Before increasing size, run multiple practice rounds and compare client total P&L, commissions/fines, entry/exit timing, and hedge turnover. Include long and short trades, forecast reversals, one-leg fills, option closes with residual stock, reset, expiry, and connection failure. Increase limits only after observing the resulting costs and losses as well as profits. Unit tests validate formulas and state behavior; live simulator execution and profitability remain unverified.

Useful settings are grouped at the top of `case1_simple.py`. Entry/exit thresholds, quantity caps, unknown volatility, hedge trigger, rate and expiry timing can be adjusted there. The program retains GET-only access; it cannot place, change or cancel a trade.
