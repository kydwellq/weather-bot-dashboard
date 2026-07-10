"""Replay the dashboard's own trade log through the Hawkeye hard gates.

Parses every trade recorded in index.html (the dashboard the fleet
publishes each cycle) and reports what the gates in trade_gate.py would
have blocked, and the P&L difference.

Usage: python3 backtest.py [path/to/index.html]
"""

from __future__ import annotations

import re
import sys

from trade_gate import kalshi_fee, FEE_EDGE_MULTIPLE, MIN_EDGE

PLACEHOLDER_FORECAST = (
    'Blended Forecast →</span> <span class="mono" '
    'style="font-weight:700;color:#e6edf3">—</span>'
)


def parse_trades(src: str):
    trades = []
    for block in re.split(r'(?=<li id="trade-)', src)[1:]:
        m = re.match(r'<li id="trade-([^"]+)"', block)
        if not m:
            continue

        def attr(name, _b=block):
            mm = re.search(r'data-%s="([^"]*)"' % name, _b[:700])
            return mm.group(1) if mm else ""

        hawk = re.search(r"\[(HAWKEYE_[A-Z0-9_]+)\]", block)
        price = re.search(r'mono">@(\d+)¢', block)
        risked = re.search(r"\$([\d.]+) risked", block)
        trades.append({
            "id": m.group(1),
            "status": attr("status"),
            "pnl": float(attr("pnl") or 0),
            "edge": float(attr("edge") or 0),
            "hawk": hawk.group(1) if hawk else "",
            "price": (int(price.group(1)) / 100.0) if price else None,
            "risked": float(risked.group(1)) if risked else 0.0,
            "placeholder": PLACEHOLDER_FORECAST in block,
        })
    return trades


def gate_reasons(t):
    """Apply the gates reconstructable from dashboard data."""
    reasons = []
    if t["placeholder"]:
        reasons.append("no forecast data")
    if t["edge"] < MIN_EDGE:
        reasons.append("edge below floor")
    if t["price"] and t["risked"]:
        contracts = max(1, round(t["risked"] / t["price"]))
        fee_fraction = kalshi_fee(t["price"], contracts) / t["risked"]
        if t["edge"] < FEE_EDGE_MULTIPLE * fee_fraction:
            reasons.append("edge below fee threshold")
    return reasons


def main(path="index.html"):
    trades = parse_trades(open(path).read())
    settled = [t for t in trades if t["status"] != "open"]

    blocked = [t for t in settled if gate_reasons(t)]
    allowed = [t for t in settled if not gate_reasons(t)]

    def pnl(ts):
        return sum(t["pnl"] for t in ts)

    print(f"trades in log:        {len(trades)}  (settled {len(settled)})")
    print(f"actual settled P&L:   ${pnl(settled):+.2f}")
    print()
    print(f"gates BLOCK:          {len(blocked)} trades  (P&L ${pnl(blocked):+.2f})")
    hb = [t for t in blocked if t["hawk"]]
    print(f"  of which hawkeye:   {len(hb)} trades  (P&L ${pnl(hb):+.2f})")
    print(f"gates ALLOW:          {len(allowed)} trades  (P&L ${pnl(allowed):+.2f})")
    ha = [t for t in allowed if t["hawk"]]
    print(f"  of which hawkeye:   {len(ha)} trades  (P&L ${pnl(ha):+.2f})")
    print()
    print("reason breakdown (blocked trades, first reason):")
    from collections import Counter
    c = Counter(gate_reasons(t)[0] for t in blocked)
    for k, v in c.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "index.html")
