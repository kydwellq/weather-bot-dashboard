# Jarvis Profitability Audit — 2026-07-11

Audit of the Kalshi weather trading fleet ("Jarvis") based on the full dashboard
snapshot in this repo (137 trades, 2026-06-23 → 2026-07-10, all settings, gates,
calibration tables, and skip logs as rendered in `index.html`).

## Headline

| Metric | Value |
|---|---|
| Realized P&L (all time) | **-$10.52** on a $137 bankroll (-9.8% on settled capital) |
| Record | 22 W / 80 L / 29 even — **22% win rate** |
| Fees paid | **$5.73 — 54% of the total loss** |
| Capital turned over | $108.75 across 137 trades (avg $1.08/trade, median $0.69) |
| Source of loss | Weather **LOW** bot: -$10.53 (127 of 137 trades). HIGH bot: +$0.01. Rain / Base-Rate-NO / Energy / Crypto: paused, 0 trades |

The fleet is not losing because weather is unpredictable. It is losing to
**three compounding, fixable problems**: fee drag at micro-stakes, a
systematically overconfident model buying longshots, and a learning loop that
never actually ran.

---

## Finding 1 — Fees eat any plausible edge at this trade size

- $5.73 of fees on $108.75 risked = **5.3% fee drag on turnover**. A weather
  model that is genuinely good might carry a 3–5% true edge; this fee load
  alone makes the whole operation negative-EV.
- **89 of 137 trades were single contracts.** Kalshi rounds fees up to the
  next cent per order, so a 1-contract trade at ~38¢ average cost pays a
  round-up penalty of several percent before the market even moves.
  The 56 settled 1-contract trades: -$1.94 P&L, of which $0.92 was fees.
- The "Minimum Net Expected Profit" gate is set to **0.020% of bankroll =
  $0.03 after fees** — deliberately set low "so high-conviction longshots can
  still trade." Those longshots are exactly what's bleeding (Finding 2).

**Fix:** trade less, bigger, or not at all.
- Raise the min-net-EV gate from $0.03 to at least **2–3× estimated round-trip
  fees** per trade.
- Stop placing 1-contract trades entirely (minimum position ~$3–5, fewer
  trades per day), or accept that sub-$5 positions are fee donations.

## Finding 2 — The longshot bleed: 70 trades, 1 win

Settled trades by entry price:

| Entry price | n | Wins | P&L |
|---|---|---|---|
| ≤ $0.25 (longshots) | **70** | **1 (1.4%)** | **-$5.40** |
| $0.25–$0.55 | 25 | 4 | -$3.63 |
| > $0.55 (favorites) | 36 | 8 | -$1.49 |
| of which > $0.80 | 10 | 3 | **+$0.66** (only profitable bucket) |

A contract priced ≤25¢ should win ~15–25% of the time *if the market is
right*. Jarvis's longshots won **1.4%** — the market was right and the model
was wrong, over and over. This is the classic favorite–longshot bias:
longshots on prediction markets are systematically **over**priced, and Jarvis
has been buying them.

Two ironies:
1. The **Base Rate NO bot** — designed to *sell* exactly these overpriced
   longshots (buy NO when YES is 4–14¢, ~85–95% expected win rate) — is
   **paused** ("V4 not applied to base_rate_no — paused 2026-05-03"). The
   fleet is running the losing side of its own best documented edge.
2. The "Max Entry Price ≤ $0.55" gate is **not being enforced** — 37 trades
   entered above $0.55 — and the data says those were the *good* trades. The
   gate is both broken and pointed the wrong way.

**Fix:** block entries below ~$0.30 unless the position is NO-side
favorite-style; validate and re-enable Base Rate NO (paper first); either
enforce entry-price gates or delete them, but don't display rules that the
engine ignores.

## Finding 3 — The edge signal is inverted

From the dashboard's own calibration table (and confirmed by re-parsing all
trades):

| Claimed edge | n | Win% | P&L |
|---|---|---|---|
| 0–10% | 36 | 17% | **+$3.04** |
| 10–20% | 63 | 24% | -$6.66 |
| 20–30% | 13 | 8% | -$2.00 |
| 30–40% | 6 | 17% | -$0.58 |
| 40–50% | 8 | **0%** | -$3.30 |
| 50%+ | 11 | **0%** | -$1.02 |

Every bucket above 10% claimed edge loses, and the biggest claimed edges win
**zero** trades. When Jarvis disagrees most with the market, Jarvis is most
wrong. A claimed 40%+ edge on a liquid weather market is almost never a real
edge — it's a stale forecast, a mis-parsed bracket, or an uncalibrated σ.

**Fix:** treat claimed edge > ~20% as an **anomaly flag, not a buy signal**
(skip + log for review). Weight sizing by *shrunken* edge (e.g. cap effective
edge at 10–15%) until TRACK 2 calibration has real data.

## Finding 4 — Probability overconfidence + trading below its own conviction bar

Dashboard probability calibration (gap = actual win rate − bucket midpoint):

| Our stated P(win) | n | Actual | Gap |
|---|---|---|---|
| 55–65% | 5 | 20% | -40pp |
| 65–75% | 8 | 14% | -56pp |
| 75–85% | 10 | **0%** | -80pp |
| 85–101% | 30 | 14% | -79pp |

And 23 settled trades were placed when the model's **own** win probability was
**below 55%** — they won 13% and lost $6.88. The HIGH bot documents a
"Min Conviction 65%" rule; the LOW bot clearly has no equivalent enforced.

**Fix:** hard gate `own_P(win) ≥ 60%` fleet-wide; widen σ globally on the LOW
model (the current 1.5–2.5× multipliers are demonstrably insufficient) until
the gap in this table is inside ±10pp.

## Finding 5 — The learning loops never closed (root cause)

This is the structural reason the bleeding never stopped:

- **LOW Bot Learning Status: "No settled LOW observations yet"** — after 121
  LOW trades and ~90 settlements. The per-settlement learning pipeline that
  should have corrected the bias/σ never received a single observation.
  Meanwhile `bias_correction` is locked at the seeded -2.0°F ("ERA5
  quarantine… locked until TRACK 1 accumulates live data").
- **TRACK 2 (edge calibration): "No calibration runs yet"** — it requires
  ≥20 settled trades *per city*, but 137 trades spread across 20 cities means
  no city ever qualifies. The overconfidence detector can mathematically never
  fire at this trade volume.
- **Attribution broken:** the "By Bot" and "By Bracket Type" breakdowns show
  `unknown` for all 137 trades, and recent trade records render with $0.00
  entries / "no decision data". You cannot tune what you don't record.

**Fix:** (1) repair LOW settlement → learning-DB ingestion; (2) change TRACK 2
to pool calibration **across cities** (a global overconfidence correction
needs ~50–100 trades total, which already exist); (3) fix bot/bracket
attribution and the null-entry trade records.

## Finding 6 — Humans are doing the exits

**62 of 137 trades (45%) ended as manual exits** (-$2.73), and the "Recent
Exits" feed is 100% 👤 MANUAL. Six automated exit rules exist (STOP_LOSS,
TIME_STOP, EDGE_FLIP…) but evidently rarely fire first. Either the thresholds
never trigger in practice or the operator doesn't trust them — both mean the
system isn't actually autonomous, and manual panic-exits near settlement
(e.g. Denver $0.31 → $0.06) realize maximum loss.

**Fix:** audit why TIME_STOP/STOP_LOSS didn't fire on the trades that were
manually killed; tighten TIME_STOP (currently only inside 3h of settlement
with P<45%) so the bot cuts losers before a human has to.

## Finding 7 — Operational health

- Every bot's last scan: **"0 markets scanned … STALE — missed a cycle …
  heartbeat says scheduler ran but no skips recorded. Check kill switch."**
  The fleet may currently be scanning nothing at all.
- `fetch_failed` appears 330 times in the skip log — roughly 9% of all skips
  are data-fetch failures, each a silent coin left on the table (or a stale
  forecast that later becomes a bad trade).
- Rain bot: paused amid mode confusion ("auto-flipped to live by
  KALSHI_MODE=live env — KY confirmed should be paper").

**Fix:** resolve the kill-switch/scheduler stall first — none of the tuning
matters if scans return 0 markets; add alerting on `fetch_failed` bursts and
on "heartbeat OK but 0 markets" cycles.

## Finding 8 — Where the losses live geographically

Worst: Denver (0/11, -$3.21), Atlanta (0/7, -$3.55), Minneapolis, Miami.
Only Phoenix is meaningfully positive (5/7, +$1.82) — a desert regime where
overnight lows are most predictable. Radiational-cooling and marine cities are
exactly where the LOW model's σ is most wrong. Per-city min-edge settings
exist but never adapted because of Finding 5.

---

## Recommended action plan (in order)

1. **Pause the LOW bot today.** It is 100% of the realized loss and its
   calibration feedback has never worked. Nothing else on this list matters
   while it trades.
2. **Fix the ops stall** (kill switch / scheduler returning 0 markets) and
   the broken recording (bot attribution, $0.00 trade records, LOW learning
   ingestion).
3. **Pool TRACK 2 calibration across cities** and run it on the existing 131
   settled trades — it will immediately quantify the σ widening needed.
4. **Add fleet-wide hard gates:** own P(win) ≥ 60%; entry price ≥ $0.30;
   claimed edge > 20% ⇒ skip-and-flag; min net EV ≥ 2× fees; no 1-contract
   orders.
5. **Re-enable Base Rate NO in paper mode** — it's the strategy aligned with
   the bias the data actually shows (longshots overpriced), and it has been
   sitting paused since May.
6. **Re-enable LOW only after** the probability-calibration gap (Finding 4)
   is inside ±10pp on paper trades.
7. **Right-size expectations:** at a $138 bankroll with Kalshi's fee
   rounding, even a perfectly calibrated fleet makes coffee money. The goal
   of this phase should be *proving calibration* (win rate matching stated
   probability), not P&L. Scale capital only after 100+ paper/live trades
   show the tables in Findings 3–4 flat.

### What "profitable" plausibly looks like

Selective favorite-side trades (entry > $0.55, model P ≥ 70%, net-EV ≥ 2×
fees) were roughly break-even *even with today's broken calibration* — that
plus a working Base Rate NO strategy is the realistic core of a profitable
Jarvis. The longshot-buying LOW bot, as configured, is structurally a losing
machine and no parameter nudge short of the gates above changes that.
