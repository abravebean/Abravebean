"""Replay past days through the exact same strategy code the live bot uses.

    python -m qqqbot.backtest --start 2025-01-01 --end 2025-11-21

For every day it:
  * builds the opening range, noise band and ATR from the data available that morning
  * runs the 10-minute checks from 10:00 to 14:00
  * on a signal, follows QQQ minute by minute for the exits
  * looks up the real option contract it would have bought (closest strike, soonest
    expiry 1-3 days out) and prices the trade from that option's 1-minute bars

Option fills are approximated by the option's 1-minute close at the entry/exit minute
(historical bid/ask isn't available), so real fills at the mid can be a little different.
Alpaca's historical option data starts in February 2024.
"""
from __future__ import annotations

import argparse
import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config, apply_overrides, load_config
from .strategy import (
    ExitTracker,
    band_stop_hit,
    build_day_setup,
    check_signal,
    regular_session,
    target_strike,
)

ET = ZoneInfo("America/New_York")
log = logging.getLogger("backtest")


# --------------------------------------------------------------------------- #
# Pure simulation (no network) - unit tested
# --------------------------------------------------------------------------- #
@dataclass
class SimTrade:
    day: date
    direction: str
    entry_time: datetime
    entry_underlying: float
    exit_time: datetime
    exit_underlying: float
    exit_reason: str
    qqq_move: float       # favourable QQQ move at exit, $
    best_move: float
    atr: float
    or_high: float
    or_low: float
    option_symbol: str = ""
    option_entry: float = math.nan
    option_exit: float = math.nan
    pnl_dollars: float = math.nan


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, ET)


def simulate_day(
    history: pd.DataFrame,
    today: pd.DataFrame,
    cfg: Config,
    session_close: Optional[datetime] = None,
) -> Optional[SimTrade]:
    """Run one day. `history` = minute bars before today, `today` = today's bars."""
    today = regular_session(today)
    if today.empty:
        return None
    d = today.index[0].date()
    if session_close is None:
        session_close = today.index[-1].to_pydatetime() + timedelta(minutes=1)
    force_exit = min(_at(d, cfg.force_exit), session_close - timedelta(minutes=15))
    last_check = min(_at(d, cfg.last_check), force_exit - timedelta(minutes=1))

    setup = build_day_setup(history, today, lookback_days=cfg.noise_lookback_days,
                            atr_days=cfg.atr_days, or_end=cfg.opening_range_end)

    # bar that CLOSES at minute m starts at m - 1min
    closes = today["close"]
    close_at = {ts.to_pydatetime() + timedelta(minutes=1): float(c) for ts, c in closes.items()}

    check = _at(d, cfg.first_check)
    entry = None
    while check <= last_check:
        upto = today[today.index <= check - timedelta(minutes=1)]
        if not upto.empty and (upto.index[-1].to_pydatetime() + timedelta(minutes=1)) == check:
            chk = check_signal(setup, upto, noise_multiplier=cfg.noise_multiplier,
                               atr_buffer=cfg.entry_atr_buffer)
            if chk.signal:
                entry = (chk.signal, check, chk.close)
                break
        check += timedelta(minutes=cfg.check_every_min)
    if entry is None:
        return None

    direction, entry_time, entry_px = entry
    tracker = ExitTracker(direction, entry_px, setup.atr, cfg.trail_activate_atr,
                          cfg.trail_giveback, cfg.stop_atr)
    first = _at(d, cfg.first_check)
    reason, exit_time, exit_px = None, None, None
    for m in sorted(t for t in close_at if t > entry_time):
        px = close_at[m]
        reason = tracker.update(px)
        if (reason is None and cfg.band_exit and m < force_exit
                and int((m - first).total_seconds() // 60) % cfg.band_exit_every_min == 0):
            chk = check_signal(setup, today[today.index <= m - timedelta(minutes=1)],
                               noise_multiplier=cfg.noise_multiplier, atr_buffer=cfg.entry_atr_buffer)
            if band_stop_hit(chk, direction):
                reason = "band_vwap_stop"
        if reason is None and m >= force_exit:
            reason = "time_exit"
        if reason:
            exit_time, exit_px = m, px
            break
    if reason is None:  # data ended early: exit on the last bar
        exit_time = max(close_at)
        exit_px = close_at[exit_time]
        reason = "end_of_data"

    return SimTrade(
        day=d, direction=direction, entry_time=entry_time, entry_underlying=entry_px,
        exit_time=exit_time, exit_underlying=exit_px, exit_reason=reason,
        qqq_move=tracker.favourable_move(exit_px), best_move=tracker.best_move,
        atr=setup.atr, or_high=setup.or_high, or_low=setup.or_low,
    )


def occ_symbol(root: str, expiry: date, direction: str, strike: float) -> str:
    """e.g. QQQ251121C00500000"""
    return f"{root}{expiry:%y%m%d}{'C' if direction == 'call' else 'P'}{int(round(strike * 1000)):08d}"


def price_at(bars: pd.DataFrame, minute_end: datetime, lookback_min: int = 10) -> float:
    """Option 1-min close for the bar closing at `minute_end` (or the last trade
    in the previous `lookback_min` minutes if that minute had no trades)."""
    if bars.empty:
        return math.nan
    window = bars[(bars.index <= minute_end - timedelta(minutes=1)) &
                  (bars.index >= minute_end - timedelta(minutes=lookback_min))]
    return float(window["close"].iloc[-1]) if not window.empty else math.nan


def summarize(trades: list[SimTrade], days_tested: int, contracts: int, months: bool = True) -> str:
    lines = [f"Days tested: {days_tested}   Trades: {len(trades)}"]
    if not trades:
        return "\n".join(lines)
    df = pd.DataFrame([asdict(t) for t in trades])
    lines.append(f"Calls: {(df.direction == 'call').sum()}   Puts: {(df.direction == 'put').sum()}")
    lines.append("Exit reasons: " + ", ".join(f"{k} {v}" for k, v in df.exit_reason.value_counts().items()))

    q = df.qqq_move
    lines.append("")
    lines.append("QQQ points (move in your direction, entry -> exit):")
    lines.append(f"  win rate {100 * (q > 0).mean():.1f}%   avg {q.mean():+.2f}   total {q.sum():+.2f}")

    opt = df.dropna(subset=["pnl_dollars"])
    lines.append("")
    if opt.empty:
        lines.append("Options: no option prices found (Alpaca option history starts Feb 2024).")
        return "\n".join(lines)
    p = opt.pnl_dollars
    equity = p.cumsum()
    dd = (equity - equity.cummax()).min()
    wins, losses = p[p > 0], p[p <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else math.inf
    lines.append(f"Options P&L ({contracts} contract(s) per trade, {len(opt)} trades priced):")
    lines.append(f"  total ${p.sum():,.0f}   avg ${p.mean():,.0f}/trade   win rate {100 * (p > 0).mean():.1f}%")
    lines.append(f"  avg win ${wins.mean() if len(wins) else 0:,.0f}   avg loss ${losses.mean() if len(losses) else 0:,.0f}"
                 f"   profit factor {pf:.2f}")
    lines.append(f"  best ${p.max():,.0f}   worst ${p.min():,.0f}   max drawdown ${dd:,.0f}")
    if not months:
        return "\n".join(lines)
    by_month = opt.assign(month=pd.to_datetime(opt.day).dt.to_period("M")).groupby("month").pnl_dollars.sum()
    lines.append("")
    lines.append("By month:")
    for m, v in by_month.items():
        lines.append(f"  {m}  ${v:>10,.0f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Data fetching (network)
# --------------------------------------------------------------------------- #
def with_retry(fn, *args, attempts: int = 5, **kwargs):
    """Call fn, retrying network hiccups (timeouts, dropped connections) with backoff."""
    import time as _time

    import requests

    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if i == attempts - 1:
                raise
            wait = 5 * 2 ** i
            log.warning("network problem (%s); retrying in %ds", type(e).__name__, wait)
            _time.sleep(wait)


class Data:
    def __init__(self, cfg: Config, cache_dir: Path, slippage: float = 0.0):
        from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        if not cfg.api_key or not cfg.secret_key:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env first.")
        self.cfg = cfg
        self.slippage = slippage
        self.stocks = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self.options = OptionHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def calendar(self, start: date, end: date) -> dict[date, datetime]:
        from alpaca.trading.requests import GetCalendarRequest

        path = self.cache_dir / f"calendar_{start}_{end}.csv"
        if path.exists():
            df = pd.read_csv(path)
            return {date.fromisoformat(d): datetime.fromisoformat(c) for d, c in zip(df.date, df.close)}
        out = {}
        for c in with_retry(self.trading.get_calendar, GetCalendarRequest(start=start, end=end)):
            cl = c.close if c.close.tzinfo else c.close.replace(tzinfo=ET)
            out[c.date] = cl.astimezone(ET)
        pd.DataFrame({"date": [d.isoformat() for d in out],
                      "close": [c.isoformat() for c in out.values()]}).to_csv(path, index=False)
        return out

    def qqq_minutes(self, start: date, end: date) -> pd.DataFrame:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        path = self.cache_dir / f"{self.cfg.symbol}_{start}_{end}.csv.gz"
        if path.exists():
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
            return df
        log.info("downloading %s minute bars %s -> %s (one-time, then cached)", self.cfg.symbol, start, end)
        req = StockBarsRequest(
            symbol_or_symbols=self.cfg.symbol, timeframe=TimeFrame.Minute,
            start=_at(start, time(0, 0)), end=_at(end + timedelta(days=1), time(0, 0)),
            feed=DataFeed.SIP,
        )
        df = with_retry(self.stocks.get_stock_bars, req).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(self.cfg.symbol, level="symbol")
        df.index = pd.to_datetime(df.index).tz_convert(ET)
        df = regular_session(df.sort_index())
        df.to_csv(path)
        return df

    def option_bars(self, symbol: str, d: date) -> pd.DataFrame:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame

        import requests

        try:
            res = with_retry(self.options.get_option_bars, OptionBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                start=_at(d, time(9, 30)), end=_at(d, time(16, 0))))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            raise  # still offline after retries: stop rather than silently skip trades
        except Exception as e:  # unknown symbol etc.
            log.debug("option bars %s failed: %s", symbol, e)
            return pd.DataFrame()
        df = res.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = pd.to_datetime(df.index).tz_convert(ET)
        return df.sort_index()

    def price_option(self, t: SimTrade, trading_days: list[date]) -> None:
        """Find the contract the bot would have bought and price entry/exit."""
        expiries = [x for x in trading_days
                    if self.cfg.min_dte <= (x - t.day).days <= self.cfg.max_dte]
        aim = target_strike(t.entry_underlying, t.direction, self.cfg.strike_offset)
        strikes = sorted({math.floor(aim), math.ceil(aim)}, key=lambda s: abs(s - aim))
        for exp in expiries:          # soonest expiry first
            for k in strikes:         # closest strike first
                sym = occ_symbol(self.cfg.symbol, exp, t.direction, k)
                bars = self.option_bars(sym, t.day)
                if bars.empty:
                    continue
                entry = price_at(bars, t.entry_time)
                exit_ = price_at(bars, t.exit_time)
                if math.isnan(entry) or math.isnan(exit_):
                    continue
                entry += self.slippage                   # pay up when buying
                exit_ = max(0.0, exit_ - self.slippage)  # give up when selling
                t.option_symbol, t.option_entry, t.option_exit = sym, entry, exit_
                t.pnl_dollars = (exit_ - entry) * 100 * self.cfg.contracts
                return


def run(cfg: Config, start: date, end: date, with_options: bool, out: Path,
        slippage: float = 0.0, split: Optional[date] = None) -> list[SimTrade]:
    data = Data(cfg, Path("data"), slippage)
    fetch_start = start - timedelta(days=45)  # warm-up for 20-day noise band + ATR
    cal = data.calendar(fetch_start, end + timedelta(days=7))
    trading_days = sorted(cal)
    bars = data.qqq_minutes(fetch_start, end)
    by_day = {d: g for d, g in bars.groupby(bars.index.date)}
    have = sorted(by_day)

    trades, tested, tested_days = [], 0, []
    for d in [x for x in trading_days if start <= x <= end and x in by_day]:
        prior = [x for x in have if x < d][-(cfg.noise_lookback_days + 5):]
        if len(prior) < max(cfg.noise_lookback_days, cfg.atr_days + 1):
            continue
        history = pd.concat([by_day[x] for x in prior])
        try:
            t = simulate_day(history, by_day[d], cfg, cal.get(d))
        except ValueError as e:  # e.g. missing opening-range bars
            log.warning("%s skipped: %s", d, e)
            continue
        tested += 1
        tested_days.append(d)
        if t is None:
            continue
        if with_options:
            data.price_option(t, trading_days)
        trades.append(t)
        log.info("%s %-4s in %s @ %.2f  out %s @ %.2f  %-17s qqq %+6.2f  option %s",
                 d, t.direction, f"{t.entry_time:%H:%M}", t.entry_underlying,
                 f"{t.exit_time:%H:%M}", t.exit_underlying, t.exit_reason, t.qqq_move,
                 "n/a" if math.isnan(t.pnl_dollars) else f"${t.pnl_dollars:+,.0f}")

    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(t) for t in trades]).to_csv(out, index=False)
    print()
    if slippage:
        print(f"Option fills include ${slippage:.2f}/share slippage each way "
              f"(${slippage * 200 * cfg.contracts:.0f} per round trip).\n")
    print(summarize(trades, tested, cfg.contracts))
    if split:
        n_before = sum(1 for x in tested_days if x < split)
        print(f"\n========== BEFORE {split} (period you may tune on) ==========")
        print(summarize([t for t in trades if t.day < split], n_before, cfg.contracts, months=False))
        print(f"\n========== FROM {split} (out-of-sample: judge changes here) ==========")
        print(summarize([t for t in trades if t.day >= split], tested - n_before, cfg.contracts, months=False))
    print(f"\nEvery trade: {out}")
    return trades


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the QQQ opening-range strategy")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2024, 3, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    ap.add_argument("--no-options", action="store_true", help="QQQ moves only, skip option pricing")
    ap.add_argument("--out", default="logs/backtest_trades.csv")
    ap.add_argument("--slippage", type=float, default=0.0,
                    help="$ per share worse fill on each option buy and sell, e.g. 0.03")
    ap.add_argument("--split", type=date.fromisoformat, default=None,
                    help="also report before/after this date separately, e.g. 2025-07-01")
    ap.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE",
                    help="override config.yaml settings for this run, e.g. --set band_exit=true max_dte=7")
    ap.add_argument("-q", "--quiet", action="store_true", help="don't print every trade")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s")
    cfg = apply_overrides(load_config(args.config), args.set)
    if args.set:
        print("Settings changed for this run:", ", ".join(args.set))
    run(cfg, args.start, args.end, not args.no_options, Path(args.out), args.slippage, args.split)


if __name__ == "__main__":
    main()
