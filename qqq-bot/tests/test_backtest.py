import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pytest

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


def test_band_exit_cuts_a_failed_breakout():
    # breakout at 10:00, then QQQ falls back under VWAP/band by 10:30
    closes = [500] * 15 + list(np.linspace(500, 515, 15)) + list(np.linspace(515, 505, 30)) + list(np.linspace(505, 496, 30)) + [496] * 300
    hist, today = flat_history(wiggle=1.0), day_bars(D, closes, start_open=500)
    held = simulate_day(hist, today, Config())
    cut = simulate_day(hist, today, Config(band_exit=True))
    assert held.exit_reason == "time_exit"
    assert cut.exit_reason == "band_vwap_stop"
    assert cut.exit_time.minute % 10 == 0 and cut.exit_time < at(11, 0)
    assert cut.qqq_move > held.qqq_move


def test_band_exit_keeps_a_winner():
    closes = [500] * 15 + list(np.linspace(500, 515, 15)) + list(np.linspace(515, 530, 360))
    t = simulate_day(flat_history(wiggle=0.5), day_bars(D, closes, start_open=500), Config(band_exit=True))
    assert t.exit_reason == "time_exit"


def test_target_strike_itm():
    from qqqbot.strategy import target_strike
    assert target_strike(500.4, "call", 2) == 498.4
    assert target_strike(500.4, "put", 2) == 502.4
    assert target_strike(500.4, "call", 0) == 500.4


def test_overrides_parse_types():
    from qqqbot.config import apply_overrides
    c = apply_overrides(Config(), ["band_exit=true", "max_dte=7", "strike_offset=2.5", "force_exit=15:30"])
    assert c.band_exit is True and c.max_dte == 7 and c.strike_offset == 2.5
    assert c.force_exit == time(15, 30)
    with pytest.raises(SystemExit):
        apply_overrides(Config(), ["nope=1"])


def test_with_retry_recovers_from_timeouts(monkeypatch):
    import requests
    import time as _time
    from qqqbot.backtest import with_retry
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("timed out")
        return "ok"

    assert with_retry(flaky) == "ok" and len(calls) == 3


def test_calendar_cache_roundtrip(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from qqqbot.backtest import Data
    d = object.__new__(Data)
    d.cache_dir = Path(tmp_path)
    close = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    d.trading = SimpleNamespace(get_calendar=lambda req: [SimpleNamespace(date=date(2026, 11, 27), close=close)])
    first = d.calendar(date(2026, 11, 1), date(2026, 11, 30))
    d.trading = None  # second call must come from the cache
    second = d.calendar(date(2026, 11, 1), date(2026, 11, 30))
    assert first == second == {date(2026, 11, 27): close}


def test_apply_capital_skips_unaffordable_and_tracks_cash():
    from qqqbot.backtest import apply_capital

    def tr(entry, exit_):
        return SimTrade(D, "call", at(10, 0), 500, at(15, 45), 501, "time_exit", 1, 1, 3, 501, 499,
                        "X", entry, exit_, (exit_ - entry) * 100)

    trades = [tr(5.0, 6.0),    # costs $500, have $600 -> take, +$100 -> $700
              tr(8.0, 9.0),    # costs $800 > $700 -> skip
              tr(7.0, 4.0),    # costs $700 -> take, -$300 -> $400
              tr(5.0, 5.5)]    # costs $500 > $400 -> skip
    lines = apply_capital(trades, 600, contracts=1)
    assert [t.qty for t in trades] == [1, 0, 1, 0]
    assert trades[2].cash_after == 400
    assert math.isnan(trades[1].pnl_dollars)
    assert "ending cash $400" in lines[0] and "skipped" in lines[1] and ": 2" in lines[1]


def test_apply_capital_caps_contracts_by_cash():
    from qqqbot.backtest import apply_capital
    t = SimTrade(D, "call", at(10, 0), 500, at(15, 45), 501, "time_exit", 1, 1, 3, 501, 499,
                 "X", 2.0, 3.0, 100.0)
    apply_capital([t], 500, contracts=5)  # can afford 2 at $200
    assert t.qty == 2 and t.pnl_dollars == 200
