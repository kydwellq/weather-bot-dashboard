# Hawkeye Remediation Kit

Built from the 2026-07-10 performance audit of the 126-trade dashboard log.

## What the audit found

| Cohort | Trades | P&L | ROI on risked |
|---|---|---|---|
| HAWKEYE_LOW_V0 | 49 | −$11.72 | −14.3% |
| HAWKEYE_H2 | 36 | −$1.45 | −12.6% |
| No Hawkeye tag | 41 | +$2.73 | +21.3% |

Root causes, in order of severity:

1. **Every Hawkeye-approved trade (85/85) had no forecast data.** Blended
   forecast null, σ null, all four model inputs empty, model weights at
   50/50/0/0 defaults — yet trades went out claiming P(win)=55% and a
   hardcoded 18% edge (all 36 H2 trades carry the identical `edge=0.1800`).
2. **Miscalibration:** mean claimed win probability 70% vs 9% realized
   across 77 settled Hawkeye trades. Trades claimed at 80–99% won 7% of
   the time. The health gates showed `model_health=D` and
   `weight_health=insufficient` and passed anyway.
3. **Fee-dominated sizing:** quarter-Kelly of a 1%-capped fraction →
   $0.33–$1.67 stakes, where Kalshi's per-contract rounded-up fee is
   ~4–6% of stake per fill. Fees were ~46% of Weather LOW's realized loss.
4. **Manual-exit churn:** 64 of 126 trades scratched early (−$2.25),
   paying spread and a second fee each time.

## What's in this kit

- `trade_gate.py` — dependency-free hard gate. Six gates: forecast data
  must exist; model health ≥ B and weights trained; edge floor; edge must
  clear 2× entry-fee drag; hardcoded-constant edge detector; calibration
  circuit breaker (halts when claimed vs realized win rate diverges >15pp
  over the trailing 20 settled trades). `python3 trade_gate.py` runs the
  self-test.
- `backtest.py` — replays the dashboard's own trade log through the
  gates. `python3 backtest.py ../index.html` currently reports:
  every one of the 118 settled trades blocked (86 no-forecast-data,
  32 zero-edge), which would have avoided the full −$12.29 loss.

## Integration (in the fleet codebase, on the machine running the bots)

1. Copy `trade_gate.py` next to the order-placement module.
2. Immediately before submitting any order, build a `TradeCandidate` from
   the signal payload (pass `None` for genuinely missing values — do not
   substitute defaults) and call `evaluate(candidate, recent_settled,
   recent_edges)`.
3. A `GateResult.allowed == False` must abort the order. Log
   `result.reasons` to the dashboard as HAWKEYE_REJECTED entries so
   filtering value becomes measurable — approvals-only logging is why the
   audit couldn't score Hawkeye's selectivity.
4. Pause `weather_low` until the forecast pipeline populates real model
   data end-to-end (the 4-Model Forecast table on trade detail pages must
   show numbers, not "—").
5. Run paper-only until the calibration breaker stays quiet for 20+
   consecutive settled paper trades — that is the definition of "the
   model earned real money" here.

## Honest scope note

These gates stop Hawkeye from losing money on signals it doesn't have.
Profit additionally requires a real forecasting edge, which can only be
proven by fixing the model-data pipeline in the fleet codebase and
passing paper-mode calibration. No gate can conjure an edge.
