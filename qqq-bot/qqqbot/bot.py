"""Main loop: wait for the open, build the day's levels, check signals, manage the trade."""
from __future__ import annotations

import argparse
import json
import logging
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .broker import Broker
from .config import Config, load_config
from .strategy import DaySetup, ExitTracker, build_day_setup, check_signal, regular_session
from .tradelog import TradeLog

ET = ZoneInfo("America/New_York")
BAR_DELAY_SEC = 8  # wait a few seconds after each minute so the bar is published
log = logging.getLogger("qqqbot")


# --------------------------------------------------------------------------- #
# Persistent state (survives a restart mid-day)
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    option_symbol: str
    qty: int
    direction: str
    entry_underlying: float
    entry_price: float
    atr: float
    best_move: float = 0.0


@dataclass
class State:
    day: str
    traded: bool = False
    position: Optional[Position] = None

    @classmethod
    def load(cls, path: Path, today: date) -> "State":
        if not path.exists():
            return cls(day=today.isoformat())
        raw = json.loads(path.read_text())
        same_day = raw.get("day") == today.isoformat()
        # an open position is kept even across days so it is never forgotten
        pos = Position(**raw["position"]) if raw.get("position") else None
        return cls(day=today.isoformat(), traded=same_day and raw.get("traded", False), position=pos)

    def save(self, path: Path) -> None:
        d = {"day": self.day, "traded": self.traded, "position": asdict(self.position) if self.position else None}
        path.write_text(json.dumps(d, indent=2))


# --------------------------------------------------------------------------- #
def at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, ET)


def sleep_until(target: datetime) -> None:
    while True:
        left = (target - datetime.now(ET)).total_seconds()
        if left <= 0:
            return
        _time.sleep(min(left, 60))


def is_check_minute(cfg: Config, minute_end: datetime, last_check: datetime) -> bool:
    first = at(minute_end.date(), cfg.first_check)
    if not (first <= minute_end <= last_check):
        return False
    return int((minute_end - first).total_seconds() // 60) % cfg.check_every_min == 0


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.broker = Broker(cfg)
        self.tlog = TradeLog(cfg.log_file, dry_run=cfg.dry_run)
        self.state_path = Path(cfg.state_file)

    # ------------------------------------------------------------- one day
    def run_forever(self) -> None:
        mode = "DRY RUN (no orders)" if self.cfg.dry_run else ("PAPER" if self.cfg.paper else "LIVE MONEY")
        log.info("starting bot: %s, %d contract(s)", mode, self.cfg.contracts)
        while True:
            try:
                self.run_day(datetime.now(ET).date())
            except KeyboardInterrupt:
                raise
            except Exception as e:  # never die silently mid-session
                log.exception("day loop crashed")
                self.tlog.write("ERROR", note=f"day loop: {e!r}")
                _time.sleep(60)
                continue
            tomorrow = datetime.now(ET).date() + timedelta(days=1)
            log.info("done for today; sleeping until %s 09:00 ET", tomorrow)
            sleep_until(at(tomorrow, time(9, 0)))

    def run_day(self, today: date) -> None:
        cfg = self.cfg
        session = self.broker.session_today(today)
        if session is None:
            log.info("%s: market closed", today)
            return
        open_, close_ = session
        force_exit = min(at(today, cfg.force_exit), close_ - timedelta(minutes=15))
        last_check = min(at(today, cfg.last_check), force_exit - timedelta(minutes=1))
        if datetime.now(ET) >= close_:
            return

        state = State.load(self.state_path, today)
        state.save(self.state_path)

        # wait until the opening range is complete
        sleep_until(at(today, cfg.opening_range_end) + timedelta(seconds=BAR_DELAY_SEC))
        setup = self.build_setup(today)

        tracker = None
        if state.position:
            p = state.position
            tracker = ExitTracker(p.direction, p.entry_underlying, p.atr, cfg.trail_activate_atr,
                                  cfg.trail_giveback, cfg.stop_atr, best_move=p.best_move)
            log.info("resuming open position %s x%d", p.option_symbol, p.qty)

        while True:
            now = datetime.now(ET)
            minute_end = now.replace(second=0, microsecond=0)
            if state.position is None and (state.traded or minute_end > last_check):
                return  # nothing left to do today
            if now >= close_:
                return

            try:
                tracker = self.on_minute(today, minute_end, setup, state, tracker, force_exit, last_check)
            except Exception as e:
                log.exception("minute step failed")
                self.tlog.write("ERROR", note=f"{minute_end:%H:%M}: {e!r}")
            state.save(self.state_path)

            sleep_until(minute_end + timedelta(minutes=1, seconds=BAR_DELAY_SEC))

    # ------------------------------------------------------------- pieces
    def build_setup(self, today: date) -> DaySetup:
        cfg = self.cfg
        hist = self.broker.history_bars(today)
        bars = self.broker.today_bars(today)
        setup = build_day_setup(hist, bars, lookback_days=cfg.noise_lookback_days,
                                atr_days=cfg.atr_days, or_end=cfg.opening_range_end)
        self.tlog.write(
            "DAY_SETUP", or_high=setup.or_high, or_low=setup.or_low, atr=setup.atr,
            call_trigger=setup.or_high + cfg.entry_atr_buffer * setup.atr,
            put_trigger=setup.or_low - cfg.entry_atr_buffer * setup.atr,
            note=f"open={setup.today_open:.2f} prev_close={setup.prev_close:.2f}",
        )
        log.info("setup: OR %.2f-%.2f ATR %.2f open %.2f prev close %.2f",
                 setup.or_low, setup.or_high, setup.atr, setup.today_open, setup.prev_close)
        return setup

    def completed_bars(self, today: date, minute_end: datetime) -> pd.DataFrame:
        bars = regular_session(self.broker.today_bars(today))
        return bars[bars.index <= minute_end - timedelta(minutes=1)]

    def on_minute(self, today, minute_end, setup, state: State, tracker, force_exit, last_check):
        cfg = self.cfg
        if state.position:
            bars = self.completed_bars(today, minute_end)
            close = float(bars["close"].iloc[-1])
            reason = tracker.update(close)
            state.position.best_move = tracker.best_move
            if reason is None and minute_end >= force_exit:
                reason = "time_exit"
            if reason:
                self.exit(state, reason, close, tracker)
                return None
            return tracker

        if not state.traded and is_check_minute(cfg, minute_end, last_check):
            bars = self.completed_bars(today, minute_end)
            chk = check_signal(setup, bars, noise_multiplier=cfg.noise_multiplier,
                               atr_buffer=cfg.entry_atr_buffer)
            self.tlog.write(
                "SIGNAL" if chk.signal else "CHECK", direction=chk.signal or "",
                qqq_close=chk.close, or_high=setup.or_high, or_low=setup.or_low, atr=setup.atr,
                upper_band=chk.upper, lower_band=chk.lower, vwap=chk.vwap,
                call_trigger=chk.call_trigger, put_trigger=chk.put_trigger,
                note=f"bar close {chk.time:%H:%M}",
            )
            log.info("%s check: close %.2f upper %.2f lower %.2f vwap %.2f -> %s",
                     minute_end.strftime("%H:%M"), chk.close, chk.upper, chk.lower, chk.vwap, chk.signal)
            if chk.signal:
                return self.enter(today, state, chk.signal, chk.close, setup)
        return tracker

    def enter(self, today, state: State, direction: str, underlying: float, setup: DaySetup):
        cfg = self.cfg
        contract = self.broker.pick_option(direction, underlying, today)
        if contract is None:
            self.tlog.write("ERROR", direction=direction, qqq_close=underlying,
                            note=f"no {direction} contract {cfg.min_dte}-{cfg.max_dte} days out")
            return None
        sym = contract.symbol
        bid, ask = self.broker.option_quote(sym)
        limit = self.broker.mid(bid, ask)
        if cfg.dry_run:
            qty, fill_px = cfg.contracts, limit
        else:
            oid = self.broker.limit_order(sym, cfg.contracts, "buy", limit)
            f = self.broker.wait_for_fill(oid, cfg.entry_fill_timeout_sec)
            qty, fill_px = f.filled_qty, f.avg_price

        if qty == 0:
            self.tlog.write("BUY_UNFILLED", direction=direction, qqq_close=underlying,
                            option_symbol=sym, qty=cfg.contracts, limit_price=limit,
                            note=f"bid {bid} ask {ask}; " + ("will retry next check" if cfg.retry_unfilled_entry else "no more trades today"))
            if not cfg.retry_unfilled_entry:
                state.traded = True
            return None

        state.traded = True
        state.position = Position(sym, qty, direction, underlying, fill_px, setup.atr)
        self.tlog.write("BUY", direction=direction, qqq_close=underlying, atr=setup.atr,
                        option_symbol=sym, qty=qty, limit_price=limit, fill_price=fill_px,
                        note=f"strike {contract.strike_price} exp {contract.expiration_date} bid {bid} ask {ask}")
        log.info("BOUGHT %d %s @ %.2f", qty, sym, fill_px)
        return ExitTracker(direction, underlying, setup.atr, cfg.trail_activate_atr,
                           cfg.trail_giveback, cfg.stop_atr)

    def exit(self, state: State, reason: str, underlying: float, tracker: ExitTracker) -> None:
        cfg = self.cfg
        p = state.position
        bid, ask = self.broker.option_quote(p.option_symbol)
        limit = self.broker.mid(bid, ask)

        if cfg.dry_run:
            sold, avg = p.qty, limit
        else:
            f = self.broker.wait_for_fill(self.broker.limit_order(p.option_symbol, p.qty, "sell", limit),
                                          cfg.exit_fill_timeout_sec)
            sold, proceeds = f.filled_qty, f.filled_qty * (f.avg_price or 0)
            left = p.qty - sold
            if left > 0:  # exits must happen: fall back to a market order
                m = self.broker.wait_for_fill(self.broker.market_order(p.option_symbol, left, "sell"), 30)
                sold += m.filled_qty
                proceeds += m.filled_qty * (m.avg_price or 0)
            avg = proceeds / sold if sold else None

        if sold < p.qty:
            p.qty -= sold
            self.tlog.write("ERROR", option_symbol=p.option_symbol, qty=p.qty, exit_reason=reason,
                            note="exit not fully filled; retrying next minute")
            return

        pnl = (avg - p.entry_price) * sold * 100
        self.tlog.write("SELL", direction=p.direction, qqq_close=underlying, atr=p.atr,
                        option_symbol=p.option_symbol, qty=sold, limit_price=limit, fill_price=avg,
                        exit_reason=reason, qqq_move=tracker.favourable_move(underlying), pnl_dollars=pnl,
                        note=f"entry {p.entry_price:.2f}; best qqq move {tracker.best_move:.2f}")
        log.info("SOLD %d %s @ %.2f (%s) P&L $%.2f", sold, p.option_symbol, avg, reason, pnl)
        state.position = None


def main() -> None:
    ap = argparse.ArgumentParser(description="QQQ opening-range options bot")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="log signals but never send orders")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True
    if not cfg.paper and not cfg.dry_run:
        log.warning("paper: false -> this will trade REAL MONEY")
    Bot(cfg).run_forever()


if __name__ == "__main__":
    main()
