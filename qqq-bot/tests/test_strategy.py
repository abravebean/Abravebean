from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from qqqbot.strategy import (
    ExitTracker,
    atr,
    band_at,
    build_day_setup,
    check_signal,
    daily_ohlc,
    minute_index,
    noise_sigma,
    opening_range,
    session_vwap,
)

ET = ZoneInfo("America/New_York")


def day_bars(d: date, closes, start_open=None, vol=1000):
    """Build regular-session minute bars from a list of closes."""
    idx = [datetime.combine(d, time(9, 30), ET) + timedelta(minutes=i) for i in range(len(closes))]
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[start_open if start_open is not None else closes[0], closes[:-1]]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + 0.05,
            "low": np.minimum(opens, closes) - 0.05,
            "close": closes,
            "volume": vol,
        },
        index=pd.DatetimeIndex(idx),
    )


def flat_history(n_days=25, price=500.0, wiggle=1.0):
    """n_days of history where every minute is `wiggle` dollars from the open."""
    frames = []
    d = date(2026, 10, 1)
    for _ in range(n_days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        closes = [price + (wiggle if i % 2 else -wiggle) for i in range(390)]
        frames.append(day_bars(d, closes, start_open=price))
        d += timedelta(days=1)
    return pd.concat(frames)


def test_minute_index():
    assert minute_index(pd.Timestamp("2026-11-25 09:30", tz=ET)) == 1
    assert minute_index(pd.Timestamp("2026-11-25 09:59", tz=ET)) == 30
    assert minute_index(pd.Timestamp("2026-11-25 15:59", tz=ET)) == 390


def test_opening_range_uses_0930_to_0945_only():
    closes = [100] * 15 + [200]  # the 09:45 bar spikes and must be ignored
    bars = day_bars(date(2026, 11, 25), closes)
    hi, lo = opening_range(bars)
    assert hi == pytest.approx(100.05)
    assert lo == pytest.approx(99.95)


def test_noise_sigma_and_band():
    hist = flat_history(wiggle=1.0)  # 1/500 = 0.2% move every minute
    sigma = noise_sigma(hist, 20)
    assert sigma.loc[30] == pytest.approx(0.002)
    up, lo = band_at(sigma, 30, today_open=500, prev_close=500, multiplier=1.25)
    assert up == pytest.approx(500 * 1.0025)
    assert lo == pytest.approx(500 * 0.9975)


def test_band_gap_uses_max_and_min():
    sigma = pd.Series({30: 0.002})
    up, lo = band_at(sigma, 30, today_open=510, prev_close=500)  # gap up
    assert up == pytest.approx(510 * 1.0025)
    assert lo == pytest.approx(500 * 0.9975)


def test_atr_simple_average_of_true_range():
    daily = pd.DataFrame(
        {"high": [11, 12, 13, 14, 15, 16], "low": [9, 10, 11, 12, 13, 14], "close": [10, 11, 12, 13, 14, 15]}
    )
    # TR each day after the first = max(2, |h-pc|=2, |l-pc|=0) = 2
    assert atr(daily, 5) == pytest.approx(2.0)


def test_vwap_volume_weighted():
    bars = day_bars(date(2026, 11, 25), [10, 20])
    bars["volume"] = [3, 1]
    bars["high"] = bars["low"] = bars["close"]
    assert session_vwap(bars) == pytest.approx((10 * 3 + 20 * 1) / 4)


def _setup_and_today(today_closes):
    hist = flat_history(wiggle=0.5)
    today = day_bars(date(2026, 11, 25), today_closes, start_open=500)
    return build_day_setup(hist, today), today


def test_call_signal_when_all_three_conditions_met():
    # flat opening range at 500, then a steady climb to well above everything
    closes = [500] * 15 + list(np.linspace(500, 515, 15))
    setup, today = _setup_and_today(closes)
    chk = check_signal(setup, today)
    assert chk.time.time() == time(10, 0)
    assert chk.close > chk.call_trigger and chk.close > chk.upper and chk.close > chk.vwap
    assert chk.signal == "call"


def test_put_signal_mirror():
    closes = [500] * 15 + list(np.linspace(500, 485, 15))
    setup, today = _setup_and_today(closes)
    assert check_signal(setup, today).signal == "put"


def test_no_signal_inside_range():
    setup, today = _setup_and_today([500] * 30)
    assert check_signal(setup, today).signal is None


def test_no_call_if_below_vwap():
    # huge volume high up early drags VWAP above the current close
    closes = [500] * 15 + [530] * 5 + list(np.linspace(530, 510, 10))
    setup, today = _setup_and_today(closes)
    today.loc[today.index[15:20], "volume"] = 1_000_000
    chk = check_signal(setup, today)
    assert chk.close > chk.call_trigger and chk.close > chk.upper
    assert chk.close < chk.vwap
    assert chk.signal is None


def test_exit_trailing_giveback_call():
    t = ExitTracker("call", entry_underlying=500, atr=1.0)
    assert t.update(503) is None          # 3 ATR, trail not active yet
    assert t.update(504) is None          # 4 ATR -> active, best = 4
    assert t.update(502.5) is None        # move 2.5 > 2.0
    assert t.update(502.0) == "trailing_giveback"


def test_exit_trailing_put_and_stop():
    t = ExitTracker("put", entry_underlying=500, atr=1.0)
    assert t.update(494) is None          # 6 ATR in favour
    assert t.update(497) == "trailing_giveback"  # gave back to 3 (half of 6)

    s = ExitTracker("call", entry_underlying=500, atr=1.0)
    assert s.update(491) is None
    assert s.update(490) == "emergency_stop"


def test_daily_ohlc_from_minutes():
    hist = flat_history(n_days=3, wiggle=1.0)
    daily = daily_ohlc(hist)
    assert len(daily) == 3
    assert daily["high"].iloc[0] == pytest.approx(501.05)
