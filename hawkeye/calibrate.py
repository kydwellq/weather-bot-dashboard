"""Derive Hawkeye's trading policy from the fleet's own trade log.

Reads the dashboard trade log (index.html) and emits hawkeye_config.json:
a policy file trade_gate.evaluate() enforces. Every decision in the
config carries its evidence (sample size, Wilson 95% CI, P&L) so the
policy is auditable and regenerates as data accrues — rerun this after
every batch of settled paper trades.

    python3 calibrate.py ../index.html          # print report + write config
    python3 calibrate.py --selftest             # offline unit test

Statistical honesty rules encoded here:
  * A strategy is DISABLED only when its 95% CI upper bound on win rate
    is below its average entry price (i.e. provably fee-negative even
    before fees) — not on vibes.
  * Segments with overlapping CIs (cities, price bands) are NOT filtered:
    tiny-n filters are overfitting, and the audit's failure was fake
    precision, not insufficient filtering.
  * Manual exits are banned only if they are both frequent and net
    negative in the observed window.
  * Everything defaults to paper mode. Promotion to live requires the
    criterion in the emitted config, checked by a human.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict

# The dashboard trade log is a rolling window, not full history. The
# config therefore records the window it was derived from.


def wilson(wins: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    hw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, center - hw), min(1.0, center + hw))


def parse_trades(src: str) -> list:
    out = {}
    for block in re.split(r'(?=<li id="trade-)', src)[1:]:
        m = re.match(r'<li id="trade-([^"]+)"', block)
        if not m:
            continue

        def attr(name, _b=block):
            mm = re.search(r'data-%s="([^"]*)"' % name, _b[:700])
            return mm.group(1) if mm else ""

        hawk = re.search(r"\[(HAWKEYE_[A-Z0-9_]+)\]", block)
        price = re.search(r'mono">@(\d+)¢', block)
        out[m.group(1)] = {
            "status": attr("status"),
            "pnl": float(attr("pnl") or 0),
            "placed": attr("placed"),
            "hawk": hawk.group(1) if hawk else "",
            "price": int(price.group(1)) / 100.0 if price else None,
        }
    return [dict(id=k, **v) for k, v in out.items()]


def build_config(trades: list) -> dict:
    settled = [t for t in trades if t["status"] not in ("open", "pending")]
    held = [t for t in settled if t["status"] in ("win", "loss")]
    dates = sorted(t["placed"][:10] for t in trades if t["placed"])

    cfg = {
        "generated_from": {
            "trades_in_window": len(trades),
            "settled": len(settled),
            "held_to_settlement": len(held),
            "window": [dates[0], dates[-1]] if dates else None,
            "note": "dashboard trade log is a rolling window, not full history",
        },
        "mode": "paper",
        "promotion_to_live_requires": (
            "calibration breaker quiet for >=20 consecutive settled paper "
            "trades AND realized paper ROI > 0 over that span"
        ),
        "strategies": {},
        "execution": {},
        "sizing": {},
    }

    # ── Strategy enable/disable from held-to-settlement evidence ──────
    strat = defaultdict(lambda: {"n": 0, "wins": 0, "price_sum": 0.0,
                                 "pnl_all_settled": 0.0, "n_all_settled": 0,
                                 "exp_wins": 0.0, "var_wins": 0.0,
                                 "n_priced": 0, "obs_wins": 0})
    for t in settled:
        if not t["hawk"]:
            continue
        s = strat[t["hawk"]]
        s["n_all_settled"] += 1
        s["pnl_all_settled"] += t["pnl"]
        if t["price"] is not None:
            # Market-implied: entry price ~= P(our side wins). Under a
            # fair market the book's positive outcomes follow a
            # Poisson-binomial with mean sum(p), var sum(p(1-p)).
            p = t["price"]
            s["n_priced"] += 1
            s["exp_wins"] += p
            s["var_wins"] += p * (1 - p)
            s["obs_wins"] += t["pnl"] > 0
        if t["status"] in ("win", "loss") and t["price"] is not None:
            s["n"] += 1
            s["wins"] += t["status"] == "win"
            s["price_sum"] += t["price"]

    for name, s in sorted(strat.items()):
        lo, hi = wilson(s["wins"], s["n"])
        avg_price = s["price_sum"] / s["n"] if s["n"] else None
        # Disable on either of two independent proofs of harm. NOTE a
        # high losing *fraction* is NOT harm — a 29c longshot book
        # rightly loses ~71% of trades. Harm is doing worse than the
        # market's own implied odds:
        #  (a) held-to-settlement win-rate CI upper bound below the avg
        #      price paid — can't be profitable even before fees;
        #  (b) adverse-selection z-test across all settled trades:
        #      positive outcomes fall >2 sigma below the market-implied
        #      expectation AND net P&L is negative.
        z = ((s["obs_wins"] - s["exp_wins"]) / math.sqrt(s["var_wins"])
             if s["var_wins"] > 0 else 0.0)
        by_winrate = s["n"] >= 4 and avg_price is not None and hi < avg_price
        by_adverse = (s["n_priced"] >= 20 and s["pnl_all_settled"] < 0
                      and z < -2.0)
        disable = by_winrate or by_adverse
        cfg["strategies"][name] = {
            "enabled": not disable,
            "paper_only": True,
            "evidence": {
                "held_n": s["n"], "held_wins": s["wins"],
                "win_rate_ci95": [round(lo, 3), round(hi, 3)],
                "avg_entry_price": round(avg_price, 3) if avg_price else None,
                "settled_n": s["n_all_settled"],
                "settled_pnl": round(s["pnl_all_settled"], 2),
                "market_expected_positive": round(s["exp_wins"], 1),
                "observed_positive": s["obs_wins"],
                "adverse_selection_z": round(z, 2),
            },
            "reason": (
                "win-rate CI upper bound below avg entry price — provably "
                "unprofitable before fees" if by_winrate else
                "adverse selection: positive outcomes >2 sigma below "
                "market-implied expectation with negative net P&L"
                if by_adverse else
                "insufficient evidence to disable; paper-only until "
                "promotion criterion met"
            ),
        }

    # ── Execution policy: manual exits ────────────────────────────────
    exits = [t for t in settled if t["status"] == "manually_exited"]
    exit_pnl = sum(t["pnl"] for t in exits)
    neg = sum(1 for t in exits if t["pnl"] < 0)
    ban = len(exits) >= 20 and exit_pnl < 0 and neg > len(exits) * 0.6
    cfg["execution"]["allow_manual_exit"] = not ban
    cfg["execution"]["evidence"] = {
        "manual_exits": len(exits), "negative": neg,
        "total_pnl": round(exit_pnl, 2),
    }
    cfg["execution"]["reason"] = (
        "manual exits are frequent and net-negative in window — hold to "
        "settlement" if ban else "insufficient evidence to ban manual exits"
    )

    # ── Sizing: from fee math (structural, not fitted) ────────────────
    # Kalshi fee = ceil(0.07*C*P*(1-P)) per fill; the ceil is worst for
    # tiny C. C>=5 keeps rounding overhead <=0.6pp of stake at mid prices.
    cfg["sizing"] = {
        "min_contracts": 5,
        "rationale": "per-contract fee ceil rounding <=0.6pp of stake at C>=5",
        "max_fraction_of_bankroll_per_trade": 0.02,
        "max_daily_loss_fraction": 0.05,
    }
    return cfg


def check_policy(candidate_strategy: str, contracts: int, is_exit: bool,
                 policy: dict) -> list:
    """Policy gate companion to trade_gate.evaluate(). Returns block
    reasons (empty = allowed). Import and call alongside evaluate()."""
    reasons = []
    strat = policy.get("strategies", {}).get(candidate_strategy)
    if strat is not None and not strat.get("enabled", True):
        reasons.append(f"strategy {candidate_strategy} disabled by policy: "
                       f"{strat.get('reason', '')}")
    if is_exit and not policy.get("execution", {}).get("allow_manual_exit", True):
        reasons.append("manual exits banned by policy — hold to settlement")
    minc = policy.get("sizing", {}).get("min_contracts", 1)
    if not is_exit and contracts < minc:
        reasons.append(f"contracts {contracts} below policy minimum {minc} "
                       f"(fee rounding drag)")
    return reasons


# ── Self-test ─────────────────────────────────────────────────────────

def _selftest():
    # Strategy that lost 0/10 held at avg price 0.5 -> disabled.
    trades = (
        [dict(id=f"a{i}", status="loss", pnl=-0.5, placed="2026-07-01T00:00",
              hawk="HAWKEYE_LOW_V0", price=0.5) for i in range(10)]
        # Strategy 5/10 at 0.5 -> stays enabled (CI spans price).
        + [dict(id=f"b{i}", status="win" if i < 5 else "loss", pnl=0.0,
                placed="2026-07-02T00:00", hawk="HAWKEYE_H2", price=0.5)
           for i in range(10)]
        # 25 manual exits, mostly negative -> banned.
        + [dict(id=f"c{i}", status="manually_exited",
                pnl=-0.1 if i < 20 else 0.05,
                placed="2026-07-03T00:00", hawk="", price=0.3)
           for i in range(25)]
    )
    cfg = build_config(trades)
    assert cfg["strategies"]["HAWKEYE_LOW_V0"]["enabled"] is False
    assert cfg["strategies"]["HAWKEYE_H2"]["enabled"] is True
    assert cfg["strategies"]["HAWKEYE_H2"]["paper_only"] is True
    assert cfg["execution"]["allow_manual_exit"] is False
    assert cfg["mode"] == "paper"

    r = check_policy("HAWKEYE_LOW_V0", 10, False, cfg)
    assert r and "disabled" in r[0], r
    r = check_policy("HAWKEYE_H2", 2, False, cfg)
    assert r and "below policy minimum" in r[0], r
    r = check_policy("HAWKEYE_H2", 10, True, cfg)
    assert r and "manual exits banned" in r[0], r
    assert check_policy("HAWKEYE_H2", 10, False, cfg) == []
    print("calibrate self-test: policy derivation and enforcement correct")


def main(path: str):
    trades = parse_trades(open(path).read())
    cfg = build_config(trades)
    out = "hawkeye_config.json"
    with open(out, "w") as f:
        json.dump(cfg, f, indent=2)
    print(json.dumps(cfg, indent=2))
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "../index.html")
