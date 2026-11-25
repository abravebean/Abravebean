import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np

from qqqbot.backtest import SimTrade, occ_symbol, price_at, simulate_day, summarize
from qqqbot.config import Config
from tests.test_strategy import day_bars, flat_history

ET = ZoneInfo("America/New_York")
D = date(2026, 11, 25)


def at(h, m):
    return datetime.combine(D, time(h, m), ET)


def test_occ_symbol():
    assert occ_symbol("QQQ", date(2025, 11, 21), "call", 500) == "QQQ251121C00500000"
    assert occ_symbol("QQQ", date(2025, 11, 21), "put", 512.5) == "QQQ251121P00512500"


def test_simulate_call_held_to_time_exit():
    closes = [500] * 15 + list(np.linspace(500, 515, 15)) + [515] * 360  # full day
    t = simulate_day(flat_history(wiggle=0.5), day_bars(D, closes, start_open=500), Config())
    assert t.direction == "call"
    assert t.entry_time == at(10, 0) and t.entry_underlying == 515
    assert t.exit_reason == "time_exit" and t.exit_time == at(15, 45)


def test_simulate_no_signal():
    t = simulate_day(flat_history(wiggle=0.5), day_bars(D, [500] * 390, start_open=500), Config())
    assert t is None


def test_simulate_only_checks_on_10_minute_marks():
    # breakout appears 10:03 and fades by 10:10 -> no trade
    closes = [500] * 30 + [500, 500, 515, 515, 515, 515, 500, 500, 500, 500] + [500] * 350
    t = simulate_day(flat_history(wiggle=0.5), day_bars(D, closes, start_open=500), Config())
    assert t is None


def test_simulate_half_day_exits_15_min_before_close():
    closes = [500] * 15 + list(np.linspace(500, 515, 15)) + [515] * 180  # until 13:00
    t = simulate_day(flat_history(wiggle=0.5), day_bars(D, closes, start_open=500), Config(),
                     session_close=at(13, 0))
    assert t.exit_time == at(12, 45)


def test_price_at_uses_last_trade_within_window():
    bars = day_bars(D, [1.0] * 30 + [2.0] + [3.0] * 10)  # bar starting 10:00 closes at 2.0
    assert price_at(bars, at(10, 1)) == 2.0
    assert math.isnan(price_at(bars, at(12, 0)))  # nothing in the last 10 min


def test_summary_runs():
    t = SimTrade(D, "call", at(10, 0), 500, at(15, 45), 505, "time_exit", 5, 6, 3, 501, 499,
                 "X", 2.0, 3.0, 100.0)
    s = summarize([t, t], 10, 1)
    assert "Trades: 2" in s and "total $200" in s
