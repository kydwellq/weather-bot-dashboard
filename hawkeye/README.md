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
- `forecast.py` — the component the fleet is missing: multi-model
  (GFS/ECMWF/ICON via Open-Meteo, free, no API key) daily low/high
  forecasts for all 15 traded cities, blended with a spread-based σ,
  plus `prob_between()` for Kalshi bracket probabilities under a normal
  model. Fails CLOSED: any fetch/parse failure yields `blended=None`,
  which the gate blocks — the audit's "trade a 55% default on null
  data" failure mode cannot recur through this path. σ priors are
  conservative and deliberately un-tuned; the calibration breaker
  enforces honesty until they're fitted on ≥100 settled paper trades.
  Offline self-test: `python3 forecast.py`. Live smoke test on the
  fleet machine: `python3 forecast.py --live austin low`.
- `calibrate.py` + `hawkeye_config.json` — data-derived trading policy,
  regenerated from the dashboard trade log. Two statistically principled
  disable criteria (a high losing fraction is NOT one of them — a 29¢
  longshot book rightly loses 71% of its trades): (a) held-to-settlement
  win-rate CI upper bound below average entry price, or (b) adverse
  selection — positive outcomes more than 2σ below the market-implied
  expectation with negative net P&L. Current policy from the data:
  HAWKEYE_LOW_V0 **disabled** (z = −7.6: market expected ~21 of its 47
  settled trades positive, observed 0); HAWKEYE_H2 enabled paper-only
  (z = −0.9: performs at market odds — no alpha without the forecast
  module, but no adverse selection); manual exits **banned** (52 of 60
  negative, −$2.73); minimum 5 contracts (fee ceil rounding); global
  paper mode with an explicit promotion-to-live criterion. Rerun
  `python3 calibrate.py path/to/index.html` after each batch of settled
  paper trades — every decision carries its evidence in the JSON.
- `collect.py` — paused-but-learning mode: a standalone cron collector
  that records each morning's multi-model forecasts and each day's
  observed outcomes for all 15 cities, with **every bot stopped** — no
  trading, no Kalshi account, no bonereaper integration. Its `report`
  command measures whether σ is honest (bias, MAE, 1σ/2σ coverage vs the
  ~68/95% targets, suggested σ multiplier) and declares CALIBRATED once
  ~100 forecast/outcome pairs pass — roughly a week at 30 pairs/day.
  That verdict is the evidence gate for restarting paper trading.
  Outcome caveat is documented in the module: outcomes are Open-Meteo
  previous-day values, close to but not the NWS settlement document —
  right for σ calibration, while settle-grade validation still comes
  from paper trades.
- `deploy_to_fleet.sh` — one-command install on the fleet machine:
  locates the bonereaper checkout, installs the modules and policy as
  `hawkeye_gate/`, runs the self-tests plus a live forecast smoke test,
  prints the order-submission call sites to wire up, and prints the
  cron lines for paused-but-learning collection.
- `backtest.py` — replays the dashboard's own trade log through the
  gates. `python3 backtest.py ../index.html` currently reports: every
  one of the 125 settled trades blocked (92 no-forecast-data, 33
  zero-edge), which would have avoided the full −$11.88 loss to date.

## Wiring example (in the fleet's signal path)

```python
from hawkeye_gate.forecast import get_forecast, prob_between
from hawkeye_gate.trade_gate import TradeCandidate, evaluate

fc = get_forecast("austin", "low")                  # real data or None
p_yes = (prob_between(fc.blended, fc.sigma, 77.5, 79.5)
         if fc.blended is not None else 0.0)
candidate = TradeCandidate(
    ticker=ticker, side="YES", price=yes_ask, contracts=n,
    claimed_win_prob=p_yes,
    blended_forecast=fc.blended, sigma=fc.sigma,
    model_inputs=fc.model_inputs,
    model_health=health_grade, weight_health=weight_state,
)
result = evaluate(candidate, recent_settled, recent_edges)
if not result.allowed:
    log_rejection(ticker, result.reasons)           # HAWKEYE_REJECTED
    return
```

## Integration (in the fleet codebase, on the machine running the bots)

1. Run `deploy_to_fleet.sh` (or copy `trade_gate.py` and `forecast.py`
   next to the order-placement module).
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
