"""Thin wrapper around alpaca-py: market data, option chain, orders."""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

from .config import Config

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

_DONE = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}


@dataclass
class Fill:
    order_id: str
    filled_qty: int
    avg_price: Optional[float]
    status: str


class Broker:
    def __init__(self, cfg: Config):
        if not cfg.api_key or not cfg.secret_key:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (see .env.example).")
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.stocks = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self.options = OptionHistoricalDataClient(cfg.api_key, cfg.secret_key)

    # ------------------------------------------------------------------ data
    def _bars(self, start: datetime, end: datetime, feed: DataFeed) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=self.cfg.symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=feed,
        )
        df = self.stocks.get_stock_bars(req).df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(self.cfg.symbol, level="symbol")
        df.index = pd.to_datetime(df.index).tz_convert(ET)
        return df.sort_index()

    def history_bars(self, today: date, calendar_days: int = 45) -> pd.DataFrame:
        """Minute bars for the days before `today`.

        History is older than 15 minutes, so free accounts may use the full SIP
        feed for it. Falls back to the live feed if SIP is refused.
        """
        start = datetime.combine(today - timedelta(days=calendar_days), datetime.min.time(), ET)
        end = datetime.combine(today, datetime.min.time(), ET)
        try:
            return self._bars(start, end, DataFeed.SIP)
        except APIError as e:
            log.warning("SIP history refused (%s); using %s feed", e, self.cfg.live_feed)
            return self._bars(start, end, DataFeed(self.cfg.live_feed))

    def today_bars(self, today: date) -> pd.DataFrame:
        start = datetime.combine(today, datetime.min.time(), ET).replace(hour=9, minute=30)
        return self._bars(start, datetime.now(ET), DataFeed(self.cfg.live_feed))

    def session_today(self, today: date) -> Optional[tuple[datetime, datetime]]:
        """(open, close) in ET for today, or None if the market is closed."""
        cal = self.trading.get_calendar(GetCalendarRequest(start=today, end=today))
        if not cal:
            return None
        c = cal[0]
        if c.date != today:
            return None
        o, cl = c.open, c.close
        o = o.replace(tzinfo=ET) if o.tzinfo is None else o.astimezone(ET)
        cl = cl.replace(tzinfo=ET) if cl.tzinfo is None else cl.astimezone(ET)
        return o, cl

    # --------------------------------------------------------------- options
    def pick_option(self, direction: str, price: float, today: date):
        """Strike closest to `price`, soonest expiry between min_dte and max_dte days out."""
        req = GetOptionContractsRequest(
            underlying_symbols=[self.cfg.symbol],
            status=AssetStatus.ACTIVE,
            type=ContractType.CALL if direction == "call" else ContractType.PUT,
            expiration_date_gte=today + timedelta(days=self.cfg.min_dte),
            expiration_date_lte=today + timedelta(days=self.cfg.max_dte),
            strike_price_gte=str(round(price * 0.97, 2)),
            strike_price_lte=str(round(price * 1.03, 2)),
            limit=1000,
        )
        contracts = [c for c in self.trading.get_option_contracts(req).option_contracts if c.tradable]
        if not contracts:
            return None
        soonest = min(c.expiration_date for c in contracts)
        same_exp = [c for c in contracts if c.expiration_date == soonest]
        return min(same_exp, key=lambda c: abs(float(c.strike_price) - price))

    def option_quote(self, symbol: str) -> tuple[float, float]:
        q = self.options.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        return float(q.bid_price), float(q.ask_price)

    @staticmethod
    def mid(bid: float, ask: float) -> float:
        if bid <= 0:
            return round(ask, 2)
        return round((bid + ask) / 2, 2)

    # ---------------------------------------------------------------- orders
    def limit_order(self, symbol: str, qty: int, side: str, price: float) -> str:
        o = self.trading.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=price,
            )
        )
        return str(o.id)

    def market_order(self, symbol: str, qty: int, side: str) -> str:
        o = self.trading.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        return str(o.id)

    def wait_for_fill(self, order_id: str, timeout_sec: int) -> Fill:
        """Poll until filled or timeout; on timeout cancel whatever is left."""
        deadline = _time.time() + timeout_sec
        while True:
            o = self.trading.get_order_by_id(order_id)
            if o.status in _DONE:
                break
            if _time.time() >= deadline:
                try:
                    self.trading.cancel_order_by_id(order_id)
                except APIError as e:  # may have filled in between
                    log.info("cancel failed (%s); re-reading order", e)
                # wait for the cancel to settle
                for _ in range(10):
                    o = self.trading.get_order_by_id(order_id)
                    if o.status in _DONE:
                        break
                    _time.sleep(1)
                break
            _time.sleep(2)
        qty = int(float(o.filled_qty or 0))
        px = float(o.filled_avg_price) if o.filled_avg_price else None
        return Fill(order_id, qty, px, str(o.status.value if hasattr(o.status, "value") else o.status))

    def open_position_qty(self, symbol: str) -> int:
        try:
            p = self.trading.get_open_position(symbol)
            return int(float(p.qty))
        except APIError:
            return 0
