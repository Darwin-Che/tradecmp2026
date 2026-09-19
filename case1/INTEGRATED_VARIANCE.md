# Integrated variance strategy

Run from the repository root:

```sh
python case1/case1_integrated_variance.py
```

When the sub-heat starts, the script reads its announced delta limit from
news. If that announcement is absent from the feed, the terminal prompts you
to enter the actual value. It sends no orders before a value is set and never
substitutes 7,000 as a default. You may supply the value explicitly with
`--announced-delta-limit 5000` (replace `5000` with that sub-heat's value).
The script sets its hard, emergency, soft, and three-sigma risk gates below
the announced value. It rechecks the news and stops if it conflicts with the
configured limit; a new sub-heat requires a new value.

`DRY_RUN = False` currently permits live orders. Set it to `True` in the Python
file for an order-printing run against actual account positions; a dry run does
not simulate fills. `AUTO_ENTRIES` and `AUTO_SIGNAL_EXITS` control automatic
entries and signal exits. Changes require a restart. Do not manually trade the
same account while a paired batch is executing.

## Signal and sizing

The model divides the 300-tick case into four 75-tick weeks. Exact analyst
volatility overrides forecasts; unknown weeks use ATM IV observed strictly
before the latest release. A missing pre-news observation can block a new
entry. Forecast and unknown-week uncertainty enter a stressed low/high
integrated-variance estimate. The $18 `MIN_ENTRY_EDGE` applies to new robust
entries. A current-week volatility carry check, call/put IV agreement, and
account risk limits also apply. New campaigns require an exact release no more
than 20 ticks old or a forecast release no more than 10 ticks old.

An existing campaign can add toward its original target whenever its **current
executable robust edge is at least $25 per straddle** (`ADD_EDGE_THRESHOLD`).
There is no test against the original entry edge and no persistence wait. The
entry window applies to new campaigns; an existing campaign can add later if
news, the current edge, carry, and limits still support its original direction.
Its target does not grow after it is set. A refreshed risk limit can shrink it.

Exact signals use a 1.5x size multiplier. When at least 80% of central remaining
variance is analyst-supported, exact signals use a 2x multiplier, regardless
of whether edge reaches $60. Coverage also affects the base target. For a
robust best-strike straddle, the campaign target is now the smaller of 3x the
old baseline target and 60% of selected-strike offline three-sigma capacity.
For a fully analyst-supported exact signal, the multipliers are 4x and 75%.
The option limits and live stress gate still apply. The calculation uses the
current option chain and a static RTM hedge; it does not assume simultaneous
fills. The full-chain optimizer remains offline only. For a
strong exact signal, the initial target fraction rises with coverage from 30%
to 100%; a fully supported exact signal can therefore start at full target,
subject to the paired-batch, three-sigma delta shock budget, and account
limits. Signals above the existing $40 strong-signal level can start with up to
50 pairs instead of 30, while weaker signals remain at 30. The same
intermediate delta checks and paired-fill verification apply; each strong
batch is no larger than the existing 50-pair exit batch. The remaining target
can be filled in subsequent batches. Forecast-only
speculative entries remain disabled by default.

## Exits and hedges

The scale-out thresholds use the displayed **robust executable edge**, not the
hold-versus-liquidate valuation. Edge above $20 can permit additions if the
$25 addition threshold is also met; $0–25 holds existing exposure without
adding. An edge below $0 on two distinct consecutive ticks reduces matched
pairs by about half once. An edge below −$8 on two distinct consecutive ticks
exits the remainder. Duplicate polls do not advance these counters. An
incompatible new volatility announcement or the expiry cutoff can trigger an
immediate full exit. Once a campaign scales out, it cannot add back. New entries
stop at tick 270 and forced exits begin at tick 275.

The default routine hedge bands are ±1,300 shares of portfolio delta for long
volatility and ±800 for short volatility. At a band boundary, the hedge aims
to neutralize option delta. With an announced ±7,000 limit, emergency hedging
takes priority at ±3,500 and the internal hard delta limit is 6,000. Both
adjust downward when the announced limit is lower. The shock
budget now follows the configured hard limit (6,000 when the announced limit
is 7,000). The 2,750 budget remains only in the pre-boost baseline size
calculation. When the options become flat, residual RTM shares are
flattened even if inside the routine band. A hedge with a stale case tick is
retried with fresh quotes; after option liquidation it can be split into
several stock batches if needed.

## Paired execution

Before the first leg, the broker reads the current case, securities, and news,
then recomputes maturity, fair volatility, executable edge, size, and risk.
A changed tick or worsened quote alone does not cancel the entry. If the case
is active, no unreviewed news arrived, direction is unchanged, current edge
passes its threshold, and limits remain safe, it proceeds at current quotes.
If the tick changes during pre-order checks, it retries valuation before
submitting. A submission with uncertain status is never retried blindly.

After a confirmed first fill, `PAIR_PENDING` or `EXIT_PENDING` exclusively owns
the pair until completion or rollback. The strategy and routine hedge wait for
that resolution. The second leg is sized to the first leg's confirmed fill.
Its **combined current edge using the actual first fill** must still meet the
entry or addition threshold; refreshed news, delta shock, and position limits
are checked. Missing-leg completion has three attempts and a six-second retry
window. If entry completion fails, the broker unwinds only the unmatched first
leg; if exit completion fails, it continues reducing the other leg. An
unresolved rollback stops execution for manual reconciliation. The API does
not offer atomic two-leg orders, so temporary exposure is possible.

## Accounting and diagnosis

Economics and the trade ledger use each security's `trading_fee` when available,
falling back to $2 per option contract and $0.02 per RTM share. The console and
JSONL audit report target pairs, maximum matched pairs, percentage filled, and
counts of additions blocked by stale ticks, worsened quotes, or a persistence
rule. The latter two should be zero with the current policy. It also reports
additions that were successfully refreshed after a tick or quote change.
These counters measure execution deployment; they do not measure the profit of
hypothetical fills. Option and stock cash-flow P&L include execution spread;
the estimated spread cost is diagnostic and is not deducted again. Simulator
fines and untracked fees are excluded from the ledger.

Decision snapshots, orders, fills, pair status, deployment counters, and trade
summaries append to `case1/integrated_variance_events.jsonl`. To review decisions
without connecting or placing orders:

```sh
python case1/case1_integrated_variance.py --replay case1/integrated_variance_events.jsonl
```

Replay uses recorded positions and cannot estimate the revised strategy's
historical P&L. Run the offline tests with:

```sh
python -m pytest case1/test_case1_integrated_variance.py case1/test_case1_simple.py -q
```
