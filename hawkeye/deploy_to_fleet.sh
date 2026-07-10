#!/usr/bin/env bash
# Deploy the Hawkeye hard gates onto the machine running the bonereaper fleet.
#
# Run ON THE FLEET MACHINE (the one that pushes "update dashboard" commits):
#
#   curl -fsSL https://raw.githubusercontent.com/kydwellq/weather-bot-dashboard/claude/hawkeye-performance-audit-aqvjen/hawkeye/deploy_to_fleet.sh | bash
#
# or clone the branch and run ./hawkeye/deploy_to_fleet.sh [path-to-bonereaper]
#
# It copies trade_gate.py into the bonereaper checkout, verifies its
# self-test there, and prints the exact call sites where evaluate() must
# be wired in. It changes no trading behavior by itself.

set -euo pipefail

BRANCH="claude/hawkeye-performance-audit-aqvjen"
RAW_BASE="https://raw.githubusercontent.com/kydwellq/weather-bot-dashboard/${BRANCH}/hawkeye"

# ── 1. Locate the bonereaper checkout ────────────────────────────────
BONE="${1:-}"
if [ -z "$BONE" ]; then
  for cand in "$HOME/bonereaper" "$HOME"/*/bonereaper "$HOME/src/bonereaper" \
              "$HOME/projects/bonereaper" "$HOME/code/bonereaper"; do
    [ -d "$cand" ] && BONE="$cand" && break
  done
fi
if [ -z "$BONE" ]; then
  BONE=$(find "$HOME" -maxdepth 4 -type d -name bonereaper -not -path '*/.*' 2>/dev/null | head -1 || true)
fi
if [ -z "$BONE" ] || [ ! -d "$BONE" ]; then
  echo "ERROR: could not find the bonereaper directory."
  echo "Re-run with the path: $0 /path/to/bonereaper"
  exit 1
fi
echo "==> bonereaper found at: $BONE"

# ── 2. Install trade_gate.py + forecast.py ───────────────────────────
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/null}")" 2>/dev/null && pwd || true)"
DEST="$BONE/hawkeye_gate"
mkdir -p "$DEST"
for f in trade_gate.py forecast.py; do
  if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/$f" ]; then
    cp "$SRC_DIR/$f" "$DEST/$f"
  else
    curl -fsSL "$RAW_BASE/$f" -o "$DEST/$f"
  fi
  echo "==> installed $DEST/$f"
done

# ── 3. Verify self-tests, then a LIVE forecast smoke test ────────────
python3 "$DEST/trade_gate.py"
python3 "$DEST/forecast.py"
echo "==> live forecast smoke test (austin low):"
python3 "$DEST/forecast.py" --live austin low || \
  echo "    live fetch failed — check outbound HTTPS to api.open-meteo.com"

# ── 4. Show where to wire it in ──────────────────────────────────────
echo
echo "==> Candidate integration points (order submission / hawkeye approval):"
grep -rn --include='*.py' -i -E \
  'def (place|submit|create)_(order|trade)|hawkeye|ai_valid|order_client\.(create|submit)' \
  "$BONE" 2>/dev/null | grep -v hawkeye_gate | head -30 || echo "  (no obvious call sites — search manually)"

cat <<'EOF'

Next steps (see hawkeye/README.md on the branch for detail):
 1. At every order-submission call site, build a TradeCandidate from the
    signal payload (pass None for missing values — never defaults) and:
        from hawkeye_gate.trade_gate import TradeCandidate, evaluate
        result = evaluate(candidate, recent_settled, recent_edges)
        if not result.allowed:
            log_rejection(candidate, result.reasons)  # -> dashboard as HAWKEYE_REJECTED
            return
 2. Pause weather_low until the forecast pipeline stops producing nulls.
 3. Run paper-only until the calibration breaker is quiet for 20+
    consecutive settled paper trades.
EOF
