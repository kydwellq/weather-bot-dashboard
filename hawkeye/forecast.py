"""Multi-model temperature forecast + bracket probability for the fleet.

This is the component the audit found missing: every Hawkeye-approved
trade was placed with a null blended forecast and a default 55%/18%-edge
probability. This module produces the real inputs a TradeCandidate needs:

    blended forecast (°F), sigma (°F), per-model inputs, and
    P(low/high lands in a Kalshi bracket) via a normal model.

Data source: Open-Meteo (free, no API key) which serves GFS, ECMWF,
ICON and HRRR in one request. Network I/O is isolated in fetch_daily();
everything else is pure math and unit-tested offline.

Offline self-test:  python3 forecast.py
Live smoke test:    python3 forecast.py --live austin low
                    (run on the fleet machine; outbound HTTPS required)

IMPORTANT — calibration honesty: SIGMA_FLOOR and the spread multiplier
below are conservative priors, not trained values. The calibration
circuit breaker in trade_gate.py is the enforcement mechanism: if these
priors are overconfident, trading halts. Replace them with fitted values
once >=100 settled paper trades exist.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Optional

# ── Cities the fleet trades (from the dashboard trade log) ────────────
CITIES = {
    "austin":        (30.2672,  -97.7431, "America/Chicago"),
    "atlanta":       (33.7490,  -84.3880, "America/New_York"),
    "dallas":        (32.7767,  -96.7970, "America/Chicago"),
    "denver":        (39.7392, -104.9903, "America/Denver"),
    "las_vegas":     (36.1699, -115.1398, "America/Los_Angeles"),
    "los_angeles":   (34.0522, -118.2437, "America/Los_Angeles"),
    "miami":         (25.7617,  -80.1918, "America/New_York"),
    "minneapolis":   (44.9778,  -93.2650, "America/Chicago"),
    "new_orleans":   (29.9511,  -90.0715, "America/Chicago"),
    "new_york":      (40.7128,  -74.0060, "America/New_York"),
    "philadelphia":  (39.9526,  -75.1652, "America/New_York"),
    "phoenix":       (33.4484, -112.0740, "America/Phoenix"),
    "san_francisco": (37.7749, -122.4194, "America/Los_Angeles"),
    "seattle":       (47.6062, -122.3321, "America/Los_Angeles"),
    "washington_dc": (38.9072,  -77.0369, "America/New_York"),
}

# Open-Meteo model identifiers. HRRR only covers CONUS short range and
# frequently returns nulls for day-ahead daily aggregates — treat any
# null as "model unavailable", never as zero.
MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless")

# σ priors (°F). NWS day-ahead min-temp MAE is ~2-3°F; model spread adds
# information but under-disperses, hence the floor.
SIGMA_FLOOR = 2.0
SPREAD_MULT = 1.0

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


@dataclass
class Forecast:
    city: str
    kind: str                       # "low" | "high"
    date: str                       # YYYY-MM-DD local
    model_inputs: dict              # model -> °F or None
    blended: Optional[float]        # °F; None when no live model
    sigma: Optional[float]          # °F; None when no live model


# ── Network I/O (isolated for testing) ────────────────────────────────

def fetch_daily(city: str, kind: str, timeout: float = 20.0) -> dict:
    """One Open-Meteo call returning per-model daily min/max for today."""
    lat, lon, tz = CITIES[city]
    var = "temperature_2m_min" if kind == "low" else "temperature_2m_max"
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": var, "models": ",".join(MODELS),
        "temperature_unit": "fahrenheit",
        "timezone": tz, "forecast_days": 2,
    })
    with urllib.request.urlopen(f"{OPEN_METEO}?{q}", timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── Pure logic ────────────────────────────────────────────────────────

def parse_daily(payload: dict, kind: str, day_index: int = 0) -> tuple:
    """Extract (date, {model: temp_or_None}) from an Open-Meteo response.

    Open-Meteo suffixes each variable with the model name, e.g.
    daily.temperature_2m_min_ecmwf_ifs025. A missing key or null entry
    means the model has no data — propagate None, NEVER substitute 0.
    """
    var = "temperature_2m_min" if kind == "low" else "temperature_2m_max"
    daily = payload.get("daily", {})
    dates = daily.get("time", [])
    date = dates[day_index] if day_index < len(dates) else None
    inputs = {}
    for m in MODELS:
        series = daily.get(f"{var}_{m}")
        v = series[day_index] if series and day_index < len(series) else None
        inputs[m] = float(v) if v is not None else None
    return date, inputs


def blend(model_inputs: dict) -> tuple:
    """(blended °F, sigma °F) from live models; (None, None) if none live."""
    live = [v for v in model_inputs.values() if v is not None]
    if not live:
        return None, None
    b = mean(live)
    spread_sigma = pstdev(live) if len(live) > 1 else 0.0
    sigma = max(SIGMA_FLOOR, SPREAD_MULT * spread_sigma)
    return b, sigma


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_between(blended: float, sigma: float, lo: Optional[float],
                 hi: Optional[float]) -> float:
    """P(settled temperature in (lo, hi]) under N(blended, sigma).

    Kalshi bracket conventions: B78.5 pays for {78, 79} → lo=77.5,
    hi=79.5 (settlement is whole °F). T-tickers are open-ended:
    pass lo=None or hi=None.
    """
    p_hi = _norm_cdf((hi - blended) / sigma) if hi is not None else 1.0
    p_lo = _norm_cdf((lo - blended) / sigma) if lo is not None else 0.0
    return max(0.0, min(1.0, p_hi - p_lo))


def get_forecast(city: str, kind: str, day_index: int = 0) -> Forecast:
    """Fetch + blend. Any failure yields a Forecast with blended=None,
    which trade_gate.evaluate() will hard-block — fail closed."""
    try:
        payload = fetch_daily(city, kind)
        date, inputs = parse_daily(payload, kind, day_index)
    except Exception:
        date, inputs = None, {m: None for m in MODELS}
    b, s = blend(inputs)
    return Forecast(city=city, kind=kind, date=date or "?",
                    model_inputs=inputs, blended=b, sigma=s)


# ── Offline self-test with a recorded Open-Meteo response shape ───────

_FIXTURE = {
    "daily": {
        "time": ["2026-07-10", "2026-07-11"],
        "temperature_2m_min_gfs_seamless":  [78.1, 77.2],
        "temperature_2m_min_ecmwf_ifs025":  [79.0, 78.0],
        "temperature_2m_min_icon_seamless": [None, 77.5],
    }
}


def _selftest():
    # Parsing: nulls stay None, values come through per model.
    date, inputs = parse_daily(_FIXTURE, "low", 0)
    assert date == "2026-07-10"
    assert inputs == {"gfs_seamless": 78.1, "ecmwf_ifs025": 79.0,
                      "icon_seamless": None}, inputs

    # Blending: mean of live models, sigma floored.
    b, s = blend(inputs)
    assert abs(b - 78.55) < 1e-9, b
    assert s == SIGMA_FLOOR, s  # spread 0.45 -> floored to 2.0

    # No live models -> None, which the gate blocks. This is the audit's
    # core failure mode, now failing CLOSED instead of trading a default.
    assert blend({"gfs_seamless": None}) == (None, None)

    # Probability: symmetric bracket around the blend ~ correct mass.
    p = prob_between(78.55, 2.0, 77.5, 79.5)
    expect = _norm_cdf((79.5 - 78.55) / 2) - _norm_cdf((77.5 - 78.55) / 2)
    assert abs(p - expect) < 1e-12
    assert 0.30 < p < 0.40, p  # ±1°F band on sigma=2 holds ~35% mass

    # Open-ended threshold sums to 1 with its complement.
    hi_side = prob_between(78.55, 2.0, None, 77.5)
    lo_side = prob_between(78.55, 2.0, 77.5, None)
    assert abs(hi_side + lo_side - 1.0) < 1e-12

    # A forecast miles from the bracket -> near-zero probability, so the
    # old "55% default" can never reappear through this path.
    assert prob_between(60.0, 2.0, 77.5, 79.5) < 1e-6

    print("forecast self-test: parsing, blending, probability all correct")


def _live(city: str, kind: str):
    fc = get_forecast(city, kind)
    print(f"{fc.city} {fc.kind} {fc.date}")
    for m, v in fc.model_inputs.items():
        print(f"  {m:15s} {v if v is not None else '—'}")
    if fc.blended is None:
        print("  NO LIVE MODEL DATA — gate would block all trades (correct)")
    else:
        print(f"  blended {fc.blended:.1f}°F  sigma {fc.sigma:.1f}°F")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--live":
        _live(sys.argv[2] if len(sys.argv) > 2 else "austin",
              sys.argv[3] if len(sys.argv) > 3 else "low")
    else:
        _selftest()
