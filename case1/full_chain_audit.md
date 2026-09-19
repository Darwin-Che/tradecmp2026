# Case 1 full-chain capacity audit

Offline DRY RUN on the latest completed three-trade run in `integrated_variance_events.jsonl`. Values are stressed **entry-value estimates**, not simulated realized P&L. All comparisons use the same recorded bid/ask quotes and flat pre-entry account.

The optimizer holds current delta within ±800 shares and every ±1, ±2, and ±3 one-tick-sigma scenario within ±6000 shares. The latter leaves a 1000-share buffer to the approximate ±7000 official limit; all scenario positions are marked using fixed quoted IV, with no full-path repricing or path-dependent hedge costs. The objective does include a simple rehedge reserve.

## Observed trades and capacity

| Entry tick | Direction / strike | Robust edge / pair | Historical target / maximum filled | Pre-boost target | Pre-boost starter / first batch | Selected-strike pair capacity | Unused pairs vs filled | Official option pair cap | Historical net P&L |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 76 | LONG 48 | $44.76 | 19 / 19 | 21 | 11 / 11 | 247 | 228 | 500 | $-145 |
| 151 | LONG 51 | $75.22 | 38 / 12 | 41 | 25 / 25 | 164 | 152 | 500 | $664 |
| 227 | SHORT 49 | $48.44 | 36 / 11 | 53 | 53 / 30 | 184 | 173 | 500 | $423 |

The latest run filled only 19, 12, and 11 matched pairs. Its targets (19, 38, 36) differ from the pre-boost baseline replay (21, 41, 53) because code and fee metadata changed after that run. This capacity is a static comparison, not a replay of the actual order path.

The official net option limit of 1000 contracts caps a same-direction straddle at 500 pairs; gross 2500 would allow 1250. The recorded snapshot has five strikes; the optimizer evaluates every available call and put, including individual legs. The official delta limit is announced by news for each sub-heat; ±7000 is the user-supplied working assumption for this audit, not a fixed competition rule. Costs use the recorded snapshot metadata or the current strategy fallbacks; verify actual fee settings before comparing with a different sub-heat.

## Sizing bottleneck by signal

| Tick | Coverage | Base from edge | After exact/forecast multiplier | After coverage factor | 3σ shock cap | Campaign cap | Net / gross pair caps | Final target |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 76 | 28.8% | 24.9 | 37.3 | 21 | 124 | 100 | 500 / 1250 | 21 |
| 151 | 42.8% | 41.8 | 62.7 | 41 | 78 | 100 | 500 / 1250 | 41 |
| 227 | 100.0% | 26.9 | 53.8 | 53 | 69 | 100 | 500 / 1250 | 53 |

The edge-scaled base and analyst coverage are the binding target restrictions at all three entries. The shock cap is 124, 78, and 69 pairs; it does not bind these pre-boost targets, but constrains expansion. The soft 3500-share and hard 6000-share delta limits are execution gates, not direct inputs to `target_size`. The starter rule and 30-pair batch cap further slow deployment. The 100%-supported tick-227 signal received the 2× exact-signal multiplier, yet its baseline target was only 53 pairs and its first batch 30.

## Boosted best-strike sizing (current code)

This keeps the selected straddle and paired execution. A 3×/60% rule applies normally; fully analyst-supported exact signals use 4×/75%. The existing live stress gate may impose an additional cap.

| Tick | Pre-boost target | Offline ±3σ pair capacity | Fraction cap | Boosted target |
|---:|---:|---:|---:|---:|
| 76 | 21 | 247 | 148 | 63 |
| 151 | 41 | 164 | 98 | 98 |
| 227 | 53 | 184 | 138 | 138 |

## Same-snapshot portfolio comparison

| Tick | Construction | Positions | Gross / net options | RTM hedge | Current delta | Robust entry edge | Multiple of old |
|---:|---|---|---:|---:|---:|---:|---:|
| 76 | Pre-boost target | LONG 21×48 C+P | 42 / +42 | +0 | -406 | $956 | 1.00× |
| 76 | Best equal pair | LONG 390×50 C+P | 780 / +780 | +24765 | -799 | $15,348 | 16.05× |
| 76 | Full chain | RTM50C +639, RTM50P +141 | 780 / +780 | -39 | -791 | $16,586 | 17.34× |
| 151 | Pre-boost target | LONG 41×51 C+P | 82 / +82 | +74 | -799 | $3,116 | 1.00× |
| 151 | Best equal pair | LONG 164×51 C+P | 328 / +328 | +2693 | -800 | $12,368 | 3.97× |
| 151 | Full chain | RTM51C +215, RTM51P +145 | 360 / +360 | +20 | -314 | $13,692 | 4.39× |
| 227 | Pre-boost target | SHORT 53×49 C+P | 106 / -106 | +0 | -226 | $2,576 | 1.00× |
| 227 | Best equal pair | SHORT 266×51 C+P | 532 / -532 | -12742 | +799 | $9,269 | 3.60× |
| 227 | Full chain | RTM49C -271, RTM51P -190 | 461 / -461 | -21 | +161 | $10,173 | 3.95× |

At equal risk bounds, most of the improvement comes from deploying more size. Across these snapshots, the best equal-call/put single-strike plan captures roughly 90–93% of the full-chain estimated edge. Strike choice and unequal legs add the remainder. The tick-76 best equal pair moves from strike 48 to 50; the full-chain plan at every tick uses unequal call and put quantities.

## Full-chain executable edge table

Fair is the central Black–Scholes model mark. Buy uses the low stressed fair value minus ask; sell uses bid minus high stressed fair value. Both deduct round-trip option commissions, an exit-spread reserve, and half of the per-straddle rehedge reserve. Values are dollars per contract.

### Tick 76: coverage 28.8%

| Ticker | Bid | Ask | Fair | Buy edge | Sell edge | Delta shares | Gamma shares/$ | Vega $/vol pt |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| RTM48C | 0.83 | 0.85 | 1.23 | +23.27 | -55.42 | +40.3 | 12.79 | 4.55 |
| RTM48P | 1.69 | 1.71 | 2.08 | +22.27 | -54.42 | -59.6 | 12.69 | 4.55 |
| RTM49C | 0.54 | 0.56 | 0.88 | +17.45 | -48.32 | +29.1 | 11.02 | 4.03 |
| RTM49P | 2.39 | 2.41 | 2.73 | +17.45 | -48.32 | -70.9 | 11.02 | 4.03 |
| RTM50C | 0.24 | 0.26 | 0.61 | +21.45 | -50.23 | +17.0 | 9.00 | 2.98 |
| RTM50P | 3.10 | 3.12 | 3.46 | +20.45 | -49.23 | -82.6 | 8.99 | 3.02 |
| RTM51C | 0.13 | 0.15 | 0.41 | +13.82 | -40.05 | +10.5 | 6.30 | 2.13 |
| RTM51P | 3.98 | 4.00 | 4.26 | +13.82 | -40.05 | -89.5 | 6.30 | 2.13 |
| RTM52C | 0.07 | 0.09 | 0.27 | +7.00 | -30.57 | +6.4 | 4.20 | 1.47 |
| RTM52P | 4.92 | 4.94 | 5.12 | +7.00 | -30.57 | -93.6 | 4.20 | 1.47 |

### Tick 151: coverage 42.8%

| Ticker | Bid | Ask | Fair | Buy edge | Sell edge | Delta shares | Gamma shares/$ | Vega $/vol pt |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| RTM48C | 2.68 | 2.70 | 2.83 | +3.07 | -25.24 | +84.6 | 9.28 | 2.44 |
| RTM48P | 0.21 | 0.23 | 0.35 | +2.07 | -24.24 | -15.8 | 9.27 | 2.47 |
| RTM49C | 1.75 | 1.77 | 2.10 | +21.67 | -46.21 | +77.2 | 14.58 | 3.10 |
| RTM49P | 0.28 | 0.30 | 0.62 | +20.67 | -45.21 | -23.1 | 14.47 | 3.13 |
| RTM50C | 1.11 | 1.13 | 1.48 | +23.62 | -49.57 | +59.7 | 17.90 | 3.98 |
| RTM50P | 0.63 | 0.65 | 1.00 | +23.62 | -49.57 | -40.3 | 17.90 | 3.98 |
| RTM51C | 0.48 | 0.50 | 1.00 | +38.04 | -64.09 | +39.4 | 21.41 | 3.95 |
| RTM51P | 1.00 | 1.02 | 1.52 | +38.04 | -64.09 | -60.6 | 21.41 | 3.95 |
| RTM52C | 0.28 | 0.30 | 0.64 | +22.51 | -47.40 | +24.2 | 15.04 | 3.20 |
| RTM52P | 1.80 | 1.82 | 2.16 | +22.51 | -47.40 | -75.8 | 15.04 | 3.20 |

### Tick 227: coverage 100.0%

| Ticker | Bid | Ask | Fair | Buy edge | Sell edge | Delta shares | Gamma shares/$ | Vega $/vol pt |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| RTM48C | 1.55 | 1.57 | 1.35 | -27.26 | +14.26 | +68.6 | 15.08 | 2.48 |
| RTM48P | 0.49 | 0.51 | 0.28 | -28.26 | +15.26 | -31.6 | 14.94 | 2.49 |
| RTM49C | 1.03 | 1.05 | 0.73 | -37.31 | +24.31 | +52.1 | 15.81 | 2.78 |
| RTM49P | 0.96 | 0.98 | 0.66 | -37.31 | +24.31 | -47.9 | 15.81 | 2.78 |
| RTM50C | 0.59 | 0.61 | 0.33 | -33.09 | +20.09 | +36.4 | 15.18 | 2.62 |
| RTM50P | 1.52 | 1.54 | 1.26 | -33.09 | +20.09 | -63.6 | 15.18 | 2.62 |
| RTM51C | 0.36 | 0.38 | 0.13 | -30.88 | +17.88 | +24.4 | 11.93 | 2.19 |
| RTM51P | 2.30 | 2.32 | 2.06 | -31.88 | +18.88 | -75.3 | 11.86 | 2.21 |
| RTM52C | 0.14 | 0.16 | 0.04 | -17.60 | +4.60 | +12.6 | 8.52 | 1.45 |
| RTM52P | 3.07 | 3.09 | 2.97 | -17.60 | +4.60 | -87.4 | 8.52 | 1.45 |

## Delta scenario audit

Scenario deltas include the chosen static RTM hedge. A one-tick sigma move uses the largest available stressed or quoted IV and `DT_YEAR`; it is a stress coordinate, not a calibrated tail probability.

| Tick | Plan | -3σ | -2σ | -1σ | Now | +1σ | +2σ | +3σ |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 76 | Current | -848 | -705 | -558 | -406 | -252 | -97 | +58 |
| 76 | Best pair | -5997 | -4446 | -2714 | -799 | +1294 | +3558 | +5982 |
| 76 | Full chain | -5984 | -4436 | -2706 | -791 | +1303 | +3570 | +5999 |
| 151 | Current | -2094 | -1694 | -1259 | -799 | -323 | +160 | +638 |
| 151 | Best pair | -5980 | -4377 | -2640 | -800 | +1105 | +3035 | +4947 |
| 151 | Full chain | -5999 | -4240 | -2333 | -314 | +1777 | +3895 | +5994 |
| 227 | Current | +1334 | +820 | +298 | -226 | -743 | -1246 | -1728 |
| 227 | Best pair | +5989 | +4427 | +2694 | +799 | -1243 | -3412 | -5685 |
| 227 | Full chain | +5999 | +4129 | +2175 | +161 | -1888 | -3945 | -5985 |

Relaxing the hard envelope to ±2σ raises modeled edge, but the resulting ±3σ deltas exceed the approximate official limit. This is a sensitivity comparison, not a recommended live profile.

| Tick | ±2σ-constrained edge | Worst ±3σ delta | ±3σ-constrained edge |
|---:|---:|---:|---:|
| 76 | $21,906 | 9,349 | $16,586 |
| 151 | $20,197 | 9,096 | $13,692 |
| 227 | $15,099 | 9,011 | $10,173 |

The old 2750-share projected 3σ shock budget is far inside the approximate official ±7000 delta boundary. The proposed offline ±6000 scenario envelope uses materially more of it while retaining 1000 shares of buffer. Scenario hedging or a tighter limit may be needed for fast moves, stale marks, and multi-tick gaps; none is modeled here.

## Equal-pair size counterfactual at the old selected strike

Initial edge below is the existing robust pair-edge estimate times size. Gamma and shock are model approximations. “Initial OK” checks recorded option/stock/current-delta limits; “3σ OK” separately checks the approximate official ±7000 delta limit after a static hedge.

| Tick | Pairs | Initial edge | Option contracts | Initial delta | Gamma | Max ±1σ delta change | Initial OK | 3σ OK |
|---:|---:|---:|---:|---:|---:|---:|---|---|
| 76 | 25 | $1,119 | 50 | -484 | +637 | 183 | yes | yes |
| 76 | 50 | $2,238 | 100 | -799 | +1274 | 367 | yes | yes |
| 76 | 100 | $4,476 | 200 | -800 | +2547 | 733 | yes | yes |
| 76 | 200 | $8,952 | 400 | -800 | +5095 | 1466 | yes | yes |
| 76 | 300 | $13,429 | 600 | -800 | +7642 | 2199 | yes | no |
| 76 | 400 | $17,905 | 800 | -799 | +10190 | 2932 | yes | no |
| 76 | 500 | $22,381 | 1000 | -799 | +12737 | 3665 | yes | no |
| 151 | 25 | $1,881 | 50 | -532 | +1071 | 290 | yes | yes |
| 151 | 50 | $3,761 | 100 | -800 | +2141 | 581 | yes | yes |
| 151 | 100 | $7,522 | 200 | -800 | +4282 | 1162 | yes | yes |
| 151 | 200 | $15,044 | 400 | -800 | +8564 | 2323 | yes | no |
| 151 | 300 | $22,566 | 600 | -799 | +12847 | 3485 | yes | no |
| 151 | 400 | $30,088 | 800 | -799 | +17129 | 4647 | yes | no |
| 151 | 500 | $37,610 | 1000 | -799 | +21411 | 5809 | yes | no |
| 227 | 25 | $1,211 | 50 | -107 | -790 | 247 | yes | yes |
| 227 | 50 | $2,422 | 100 | -213 | -1581 | 494 | yes | yes |
| 227 | 100 | $4,844 | 200 | -426 | -3161 | 988 | yes | yes |
| 227 | 200 | $9,688 | 400 | -800 | -6323 | 1977 | yes | yes |
| 227 | 300 | $14,533 | 600 | -799 | -9484 | 2965 | yes | no |
| 227 | 400 | $19,377 | 800 | -800 | -12645 | 3953 | yes | no |
| 227 | 500 | $24,221 | 1000 | -799 | -15807 | 4942 | yes | no |

At 500 pairs on each of the three chosen strikes, summed initial robust edge is about $84k, but all three static portfolios breach ±7000 under at least one ±3σ move. At the proposed ±6000/3σ bound, the full-chain optimizer’s summed estimated entry edge is about $40k. Neither figure predicts realized case profit: future volatility, edge decay, timing, hedge P&L, and order execution determine the result.

## Recommendation and limits

Keep the live paired execution, actual-fill reconciliation, news invalidation, and ledger unchanged. Test staged larger campaigns offline with a current-delta target of ±800, hard current and ±1/2/3σ scenario envelope of ±6000, official option/stock constraints, per-order 100-contract and 10,000-share splits, and fresh quote/news checks before every child order. Recompute scenario risk after every actual fill and hedge; avoid sending a full target as if fills were atomic.

This audit supports under-deployment as a major source of foregone modeled edge. It does not establish that the volatility forecast is calibrated or that a larger portfolio would have made $100k per sub-heat. The historical three trades yielded only about $942 total net, including a first-trade loss and large stock-hedge losses; scaling those realized paths could amplify losses. The offline optimizer makes no live API calls or orders.
