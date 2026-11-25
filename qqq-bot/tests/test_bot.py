"""Drive Bot.on_minute end-to-end with a fake broker (no network)."""
import csv
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

from qqqbot.bot import Bot, State
from qqqbot.broker import Broker, Fill
from qqqbot.config import Config
from qqqbot.strategy import build_day_setup
from qqqbot.tradelog import TradeLog
from tests.test_strategy import day_bars, flat_history

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 25)


class FakeBroker:
    mid = staticmethod(Broker.mid)

    def __init__(self, today_bars, fill_buy=True):
        self.bars = today_bars
        self.fill_buy = fill_buy
        self.orders = []

    def today_bars(self, today):
        return self.bars

    def pick_option(self, direction, price, today):
        return SimpleNamespace(symbol="QQQ261126C00515000", strike_price=515.0,
                               expiration_date=date(2026, 11, 26))

    def option_quote(self, symbol):
        return 2.00, 2.20

    def limit_order(self, symbol, qty, side, price):
        self.orders.append(("limit", side, qty, price))
        return f"o{len(self.orders)}"

    def market_order(self, symbol, qty, side):
        self.orders.append(("market", side, qty, None))
        return f"o{len(self.orders)}"

    def wait_for_fill(self, oid, timeout):
        kind, side, qty, price = self.orders[int(oid[1:]) - 1]
        if side == "buy" and not self.fill_buy:
            return Fill(oid, 0, None, "canceled")
        if side == "sell" and kind == "limit":  # exit limit doesn't fill -> market fallback
            return Fill(oid, 0, None, "canceled")
        return Fill(oid, qty, price if price else 3.10, "filled")


def make_bot(tmp_path, broker, **cfg_kw):
    bot = object.__new__(Bot)
    bot.cfg = Config(contracts=2, log_file=str(tmp_path / "t.csv"), state_file=str(tmp_path / "s.json"), **cfg_kw)
    bot.broker = broker
    bot.tlog = TradeLog(bot.cfg.log_file, bot.cfg.dry_run)
    return bot


def rows(tmp_path):
    with open(tmp_path / "t.csv") as f:
        return list(csv.DictReader(f))


def at(h, m):
    return datetime.combine(TODAY, time(h, m), ET)


def test_full_trade_cycle(tmp_path):
    closes = [500] * 15 + list(np.linspace(500, 515, 15))  # breakout by 10:00
    bars = day_bars(TODAY, closes, start_open=500)
    setup = build_day_setup(flat_history(wiggle=0.5), bars)
    broker = FakeBroker(bars)
    bot = make_bot(tmp_path, broker)
    state = State(day=TODAY.isoformat())

    tracker = bot.on_minute(TODAY, at(10, 0), setup, state, None, at(15, 45), at(14, 0))
    assert state.traded and state.position.qty == 2
    assert broker.orders[0] == ("limit", "buy", 2, 2.10)  # mid of 2.00 / 2.20

    # later minutes: price holds, then force exit at 15:45
    broker.bars = day_bars(TODAY, closes + [515] * 10, start_open=500)
    tracker = bot.on_minute(TODAY, at(10, 30), setup, state, tracker, at(15, 45), at(14, 0))
    assert state.position is not None
    bot.on_minute(TODAY, at(15, 45), setup, state, tracker, at(15, 45), at(14, 0))
    assert state.position is None
    assert broker.orders[-1] == ("market", "sell", 2, None)  # limit didn't fill -> market

    events = [r["event"] for r in rows(tmp_path)]
    assert events == ["SIGNAL", "BUY", "SELL"]
    sell = rows(tmp_path)[-1]
    assert sell["exit_reason"] == "time_exit"
    assert float(sell["pnl_dollars"]) == round((3.10 - 2.10) * 2 * 100, 4)


def test_no_check_off_schedule(tmp_path):
    closes = [500] * 15 + list(np.linspace(500, 515, 16))  # last bar closes 10:01
    bars = day_bars(TODAY, closes, start_open=500)
    setup = build_day_setup(flat_history(wiggle=0.5), bars)
    bot = make_bot(tmp_path, FakeBroker(bars))
    state = State(day=TODAY.isoformat())
    bot.on_minute(TODAY, at(10, 1), setup, state, None, at(15, 45), at(14, 0))
    assert not state.traded and rows(tmp_path) == []


def test_unfilled_entry_retries(tmp_path):
    closes = [500] * 15 + list(np.linspace(500, 515, 15))
    bars = day_bars(TODAY, closes, start_open=500)
    setup = build_day_setup(flat_history(wiggle=0.5), bars)
    bot = make_bot(tmp_path, FakeBroker(bars, fill_buy=False))
    state = State(day=TODAY.isoformat())
    bot.on_minute(TODAY, at(10, 0), setup, state, None, at(15, 45), at(14, 0))
    assert not state.traded and state.position is None
    assert rows(tmp_path)[-1]["event"] == "BUY_UNFILLED"


def test_state_roundtrip(tmp_path):
    from qqqbot.bot import Position
    p = tmp_path / "s.json"
    State(day=TODAY.isoformat(), traded=True,
          position=Position("X", 1, "call", 500, 2.1, 7.5, 3.0)).save(p)
    s = State.load(p, TODAY)
    assert s.traded and s.position.best_move == 3.0
    s2 = State.load(p, TODAY + timedelta(days=1))  # new day: traded resets, position kept
    assert not s2.traded and s2.position is not None
