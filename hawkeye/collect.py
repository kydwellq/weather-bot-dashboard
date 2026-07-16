"""Zero-risk data collector: pause the bots, keep the learning.

Runs the full Hawkeye decision inputs — multi-model forecasts, blend,
sigma — and later joins them against observed outcomes, WITHOUT placing
any trades and WITHOUT needing the bonereaper process at all. Run it
from cron on any machine with outbound HTTPS while every bot stays
paused. The dataset it builds is exactly what the promotion criterion
needs: proof that claimed probabilities match reality.

Usage (on the fleet machine or anywhere):
    python3 collect.py forecasts   # run each morning ~07:00 UTC (pre-dawn)
    python3 collect.py outcomes    # run each afternoon for yesterday's actuals
    python3 collect.py report      # calibration report from collected pairs
    python3 collect.py --selftest  # offline unit test

Suggested cron (UTC):
    0 7  * * *  cd /path/to/hawkeye_gate && python3 collect.py forecasts
    0 20 * * *  cd /path/to/hawkeye_gate && python3 collect.py outcomes

Data lands in ./data/forecasts.jsonl and ./data/outcomes.jsonl.

Outcome-source caveat, stated honestly: Kalshi settles on NWS Daily
Climate Reports; the outcomes here are Open-Meteo's previous-day values,
which track NWS station obs closely but are not the settlement document.
They are plenty for sigma calibration (the question is "are our
probabilities honest", not "would this exact trade have settled YES").
Final settle-grade validation still comes from real paper trades.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from forecast import CITIES, get_forecast

DATA_DIR = os.environ.get("HAWKEYE_DATA_DIR", "data")


def _append(path: str, rec: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _load(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ── forecasts: record what the model believes, before outcomes exist ──

def collect_forecasts(data_dir: str = DATA_DIR):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for city in CITIES:
        for kind in ("low", "high"):
            fc = get_forecast(city, kind)
            _append(os.path.join(data_dir, "forecasts.jsonl"), {
                "ts": ts, "date": fc.date, "city": city, "kind": kind,
                "model_inputs": fc.model_inputs,
                "blended": fc.blended, "sigma": fc.sigma,
            })
            n += 1
            status = ("no data" if fc.blended is None
                      else f"{fc.blended:.1f}F sigma {fc.sigma:.1f}")
            print(f"  {city:14s} {kind:4s} {fc.date} {status}")
    print(f"recorded {n} forecasts")


# ── outcomes: yesterday's observed min/max per city ───────────────────

def fetch_observed(city: str, day: str, timeout: float = 20.0) -> dict:
    lat, lon, tz = CITIES[city]
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_min,temperature_2m_max",
        "temperature_unit": "fahrenheit", "timezone": tz,
        "past_days": 3, "forecast_days": 1,
    })
    with urllib.request.urlopen(
            f"https://api.open-meteo.com/v1/forecast?{q}", timeout=timeout) as r:
        payload = json.loads(r.read().decode())
    daily = payload.get("daily", {})
    times = daily.get("time", [])
    if day not in times:
        return {}
    i = times.index(day)
    lo = daily.get("temperature_2m_min", [None] * len(times))[i]
    hi = daily.get("temperature_2m_max", [None] * len(times))[i]
    return {"low": lo, "high": hi}


def collect_outcomes(data_dir: str = DATA_DIR, day: str = None):
    day = day or (date.today() - timedelta(days=1)).isoformat()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for city in CITIES:
        try:
            obs = fetch_observed(city, day)
        except Exception as e:
            print(f"  {city}: fetch failed ({e})")
            continue
        for kind in ("low", "high"):
            v = obs.get(kind)
            if v is None:
                continue
            _append(os.path.join(data_dir, "outcomes.jsonl"), {
                "ts": ts, "date": day, "city": city, "kind": kind,
                "observed": float(v),
            })
            n += 1
    print(f"recorded {n} outcomes for {day}")


# ── report: is sigma honest? (pure math, unit-tested) ─────────────────

def calibration_stats(forecasts: list, outcomes: list) -> dict:
    obs = {(o["date"], o["city"], o["kind"]): o["observed"] for o in outcomes}
    # keep the earliest forecast per key (the pre-dawn belief, not later
    # revisions) so calibration measures the tradeable forecast
    first = {}
    for f in sorted(forecasts, key=lambda f: f["ts"]):
        key = (f["date"], f["city"], f["kind"])
        if key not in first and f.get("blended") is not None:
            first[key] = f
    pairs = [(f, obs[k]) for k, f in first.items() if k in obs]
    if not pairs:
        return {"n": 0}
    errs = [(o - f["blended"]) for f, o in pairs]
    zs = [(o - f["blended"]) / f["sigma"] for f, o in pairs]
    n = len(pairs)
    bias = sum(errs) / n
    mae = sum(abs(e) for e in errs) / n
    cov1 = sum(1 for z in zs if abs(z) <= 1) / n
    cov2 = sum(1 for z in zs if abs(z) <= 2) / n
    zvar = sum(z * z for z in zs) / n
    per_kind = defaultdict(list)
    for f, o in pairs:
        per_kind[f["kind"]].append(o - f["blended"])
    return {
        "n": n, "bias_F": round(bias, 2), "mae_F": round(mae, 2),
        "coverage_1sigma": round(cov1, 3), "coverage_2sigma": round(cov2, 3),
        "sigma_multiplier_suggested": round(math.sqrt(zvar), 2),
        "bias_by_kind": {k: round(sum(v) / len(v), 2)
                         for k, v in per_kind.items()},
        "verdict": _verdict(n, bias, cov1, cov2),
    }


def _verdict(n, bias, cov1, cov2):
    if n < 100:
        return f"insufficient data ({n}/100 pairs) — keep collecting"
    problems = []
    if abs(bias) > 1.0:
        problems.append(f"bias {bias:+.1f}F — apply bias correction")
    if not 0.60 <= cov1 <= 0.76:
        problems.append(f"1-sigma coverage {cov1:.0%} (target ~68%) — "
                        "rescale sigma by the suggested multiplier")
    if cov2 < 0.90:
        problems.append(f"2-sigma coverage {cov2:.0%} — heavy tails, widen sigma")
    return ("CALIBRATED — probabilities are honest; paper trading may begin"
            if not problems else "; ".join(problems))


def report(data_dir: str = DATA_DIR):
    stats = calibration_stats(
        _load(os.path.join(data_dir, "forecasts.jsonl")),
        _load(os.path.join(data_dir, "outcomes.jsonl")))
    print(json.dumps(stats, indent=2))


# ── self-test ─────────────────────────────────────────────────────────

def _selftest():
    fcs, outs = [], []
    # 120 pairs, forecast 80F sigma 2F; outcomes alternate +1/-1F
    # (bias 0, all within 1 sigma), plus 20 outliers at +5F (outside 2 sigma).
    for i in range(120):
        d = f"day{i:03d}"
        city = list(CITIES)[i % len(CITIES)]
        fcs.append({"ts": f"T{i:03d}", "date": d, "city": city, "kind": "low",
                    "blended": 80.0, "sigma": 2.0})
        # a later revision the report must ignore
        fcs.append({"ts": f"T9{i:03d}", "date": d, "city": city, "kind": "low",
                    "blended": 999.0, "sigma": 2.0})
        outs.append({"ts": "x", "date": d, "city": city, "kind": "low",
                     "observed": 80.0 + (5.0 if i >= 100 else (1 if i % 2 else -1))})
    s = calibration_stats(fcs, outs)
    assert s["n"] == 120, s
    assert abs(s["bias_F"] - (100 * 0 + 20 * 5) / 120) < 0.01, s   # 0.83F
    assert s["coverage_1sigma"] == round(100 / 120, 3), s
    assert "coverage" in s["verdict"] or "CALIBRATED" in s["verdict"], s

    # perfect calibration -> CALIBRATED verdict
    fcs2 = [{"ts": f"T{i}", "date": f"d{i}", "city": "austin", "kind": "low",
             "blended": 80.0, "sigma": 2.0} for i in range(100)]
    # outcomes spread like a ~normal: 68 within 1 sigma, rest within 2
    outs2 = [{"ts": "x", "date": f"d{i}", "city": "austin", "kind": "low",
              "observed": 80.0 + (0.5 if i < 68 else (3.0 if i % 2 else -3.0))}
             for i in range(100)]
    s2 = calibration_stats(fcs2, outs2)
    assert s2["verdict"].startswith("CALIBRATED"), s2

    assert calibration_stats([], []) == {"n": 0}
    print("collect self-test: calibration math and verdicts correct")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "--selftest":
        _selftest()
    elif cmd == "forecasts":
        collect_forecasts()
    elif cmd == "outcomes":
        collect_outcomes(day=sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "report":
        report()
    else:
        print(__doc__)
