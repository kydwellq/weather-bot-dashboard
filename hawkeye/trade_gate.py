"""Hawkeye hard trade gate — reference implementation.

Drop-in pre-trade validator for the weather fleet. Every candidate trade
must pass ALL gates before Hawkeye is allowed to approve it. Designed to
fix the failure modes found in the 2026-07-10 performance audit:

  1. 85/85 Hawkeye-approved trades were placed with NO forecast data
     (blended forecast null, sigma null) using a default 55% probability
     and a hardcoded 18% edge.
  2. Claimed win probability averaged 70% vs 9% realized — the health
     gates (model_health=D, weight_health=insufficient) passed anyway.
  3. Average stake $0.33-$1.67 meant Kalshi's per-contract rounded-up fee
     consumed ~46% of losses; no fee-awareness existed in edge math.

Pure Python, no dependencies. Integrate by constructing a TradeCandidate
from the bot's signal payload and calling evaluate(); a BLOCK result on
any gate must abort the order, not just log a warning.

Self-test: `python3 trade_gate.py`
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence


# ── Tunables ──────────────────────────────────────────────────────────
# Minimum acceptable model health grade (A best). D-grade models placed
# 85 losing trades; require B or better to trade real money.
MIN_MODEL_HEALTH = "B"

# Edge must clear the entry-fill fee by this multiple before a trade is
# worth placing. 2x means: half the claimed edge could be wrong and the
# trade still isn't fee-negative. Holding to settlement pays only the
# entry fee; early exits pay a second fill, which is another reason the
# manual-exit habit has to stop.
FEE_EDGE_MULTIPLE = 2.0

# Absolute floor on claimed edge regardless of fees.
MIN_EDGE = 0.05

# Calibration circuit breaker: with at least MIN_CALIBRATION_SAMPLE
# settled trades, if mean claimed win prob exceeds realized win rate by
# more than MAX_CALIBRATION_GAP, halt all trading until recalibrated.
# (Audit measured a 0.61 gap: 70% claimed vs 9% realized.)
MIN_CALIBRATION_SAMPLE = 20
MAX_CALIBRATION_GAP = 0.15

# Suspicious-constant detector: if this many consecutive candidates
# arrive with the identical claimed edge, treat it as a hardcoded
# default, not a computed signal. (All 36 H2 trades carried edge=0.1800.)
CONSTANT_EDGE_RUN = 5

KALSHI_FEE_RATE = 0.07  # ceil(0.07 * P * (1-P)) per contract, per fill


@dataclass
class SettledTrade:
    """Minimal record of a settled trade for the calibration breaker."""
    claimed_win_prob: float  # probability the model claimed at entry
    won: bool


@dataclass
class TradeCandidate:
    ticker: str
    side: str                                # "YES" or "NO"
    price: float                             # ask for our side, dollars
    contracts: int
    claimed_win_prob: float                  # model's P(our side wins)
    blended_forecast: Optional[float]        # °F; None = no forecast data
    sigma: Optional[float]                   # forecast uncertainty; None = missing
    model_inputs: dict = field(default_factory=dict)   # model -> forecast °F (None = missing)
    model_health: Optional[str] = None       # letter grade "A".."F"
    weight_health: Optional[str] = None      # e.g. "ok" / "insufficient"

    @property
    def claimed_edge(self) -> float:
        return self.claimed_win_prob - self.price


@dataclass
class GateResult:
    allowed: bool
    reasons: list  # human-readable block reasons; empty when allowed


def kalshi_fee(price: float, contracts: int) -> float:
    """Kalshi taker fee in dollars: ceil-to-cent of 0.07 * C * P * (1-P)."""
    raw_cents = KALSHI_FEE_RATE * contracts * price * (1.0 - price) * 100.0
    return math.ceil(raw_cents - 1e-9) / 100.0


def round_trip_fee(price: float, contracts: int) -> float:
    """Worst-case fees: entry fill + early exit fill at similar price.

    Holding to settlement pays only the entry fee, but the fleet manually
    exits over half its positions, so budget for both fills.
    """
    return 2.0 * kalshi_fee(price, contracts)


def evaluate(
    candidate: TradeCandidate,
    recent_settled: Sequence[SettledTrade] = (),
    recent_edges: Sequence[float] = (),
) -> GateResult:
    """Run every hard gate. Any failure blocks the trade.

    recent_settled: trailing settled trades (newest last) for the
        calibration circuit breaker.
    recent_edges: claimed edges of the most recent candidates (newest
        last) for the hardcoded-default detector.
    """
    reasons = []

    # Gate 1 — forecast data must exist. This alone would have blocked
    # every Hawkeye loss in the audit window.
    if candidate.blended_forecast is None:
        reasons.append("no blended forecast — refusing to trade on placeholder probability")
    if candidate.sigma is None:
        reasons.append("no forecast uncertainty (sigma) — probability cannot have been computed")
    live_models = [m for m, v in candidate.model_inputs.items() if v is not None]
    if not live_models:
        reasons.append("zero live model inputs (GFS/ECMWF/HRRR/ICON all missing)")

    # Gate 2 — model health must be a real gate, not decoration.
    if candidate.model_health is not None and candidate.model_health > MIN_MODEL_HEALTH:
        reasons.append(
            f"model_health={candidate.model_health} worse than required {MIN_MODEL_HEALTH}"
        )
    if candidate.weight_health == "insufficient":
        reasons.append("weight_health=insufficient — model weights are untrained defaults")

    # Gate 3 — sane probability and edge floor.
    if not (0.0 < candidate.claimed_win_prob < 1.0):
        reasons.append(f"claimed win prob {candidate.claimed_win_prob} out of range")
    if candidate.claimed_edge < MIN_EDGE:
        reasons.append(
            f"claimed edge {candidate.claimed_edge:+.3f} below floor {MIN_EDGE:.2f}"
        )

    # Gate 4 — fee-aware economics. Expected gross edge per dollar risked
    # must exceed entry fee drag by FEE_EDGE_MULTIPLE. The per-contract
    # ceil rounding makes tiny positions disproportionately expensive.
    stake = candidate.price * candidate.contracts
    if stake > 0:
        fee_fraction = kalshi_fee(candidate.price, candidate.contracts) / stake
        if candidate.claimed_edge < FEE_EDGE_MULTIPLE * fee_fraction:
            reasons.append(
                f"edge {candidate.claimed_edge:+.3f} does not clear "
                f"{FEE_EDGE_MULTIPLE:.0f}x fee drag ({fee_fraction:.3f}/$ risked) — "
                f"size up or skip"
            )
    else:
        reasons.append("zero stake")

    # Gate 5 — hardcoded-default detector.
    edges = list(recent_edges) + [candidate.claimed_edge]
    if len(edges) >= CONSTANT_EDGE_RUN and len(set(round(e, 4) for e in edges[-CONSTANT_EDGE_RUN:])) == 1:
        reasons.append(
            f"last {CONSTANT_EDGE_RUN} candidates share identical edge "
            f"{candidate.claimed_edge:.4f} — looks like a default constant, not a signal"
        )

    # Gate 6 — calibration circuit breaker on realized results.
    settled = list(recent_settled)
    if len(settled) >= MIN_CALIBRATION_SAMPLE:
        claimed = sum(t.claimed_win_prob for t in settled) / len(settled)
        realized = sum(1 for t in settled if t.won) / len(settled)
        gap = claimed - realized
        if gap > MAX_CALIBRATION_GAP:
            reasons.append(
                f"calibration breaker: claimed {claimed:.0%} vs realized {realized:.0%} "
                f"over last {len(settled)} settled trades (gap {gap:.0%} > "
                f"{MAX_CALIBRATION_GAP:.0%}) — halt and recalibrate"
            )

    return GateResult(allowed=not reasons, reasons=reasons)


# ── Self-test ─────────────────────────────────────────────────────────

def _selftest():
    good = TradeCandidate(
        ticker="KXHIGHNY-26JUL10-B90.5", side="YES", price=0.40, contracts=25,
        claimed_win_prob=0.58, blended_forecast=90.7, sigma=1.4,
        model_inputs={"GFS": 90.4, "ECMWF": 91.0}, model_health="B",
        weight_health="ok",
    )
    r = evaluate(good)
    assert r.allowed, r.reasons

    # The exact failure mode from the audit: placeholder data + default edge.
    audit_case = TradeCandidate(
        ticker="KXLOWTNOLA-26JUL09-B78.5", side="YES", price=0.37, contracts=1,
        claimed_win_prob=0.55, blended_forecast=None, sigma=None,
        model_inputs={"GFS": None, "ECMWF": None, "HRRR": None, "ICON": None},
        model_health="D", weight_health="insufficient",
    )
    r = evaluate(audit_case)
    assert not r.allowed
    assert len(r.reasons) >= 5, r.reasons

    # Fee gate: 1 contract at 37c with 6pp edge — fees eat it.
    fee_case = TradeCandidate(
        ticker="X", side="YES", price=0.37, contracts=1,
        claimed_win_prob=0.43, blended_forecast=75.0, sigma=1.0,
        model_inputs={"GFS": 75.0}, model_health="A", weight_health="ok",
    )
    r = evaluate(fee_case)
    assert not r.allowed and any("fee drag" in x for x in r.reasons), r.reasons

    # Same edge, 25 contracts — per-contract ceil rounding amortizes, passes.
    fee_case_sized = TradeCandidate(
        ticker="X", side="YES", price=0.37, contracts=25,
        claimed_win_prob=0.48, blended_forecast=75.0, sigma=1.0,
        model_inputs={"GFS": 75.0}, model_health="A", weight_health="ok",
    )
    r = evaluate(fee_case_sized)
    assert r.allowed, r.reasons

    # Calibration breaker: 70% claimed, 10% realized.
    history = [SettledTrade(0.70, i < 2) for i in range(20)]
    r = evaluate(good, recent_settled=history)
    assert not r.allowed and any("calibration breaker" in x for x in r.reasons), r.reasons

    # Constant-edge detector.
    r = evaluate(good, recent_edges=[good.claimed_edge] * 4)
    assert not r.allowed and any("default constant" in x for x in r.reasons), r.reasons

    print("trade_gate self-test: all gates behave as specified")


if __name__ == "__main__":
    _selftest()
