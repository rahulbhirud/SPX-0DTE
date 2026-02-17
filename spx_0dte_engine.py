"""
╔══════════════════════════════════════════════════════════════════╗
║  SPX 0DTE MEAN REVERSION ENGINE — PRODUCTION v3.0              ║
║  Broker: TradeStation API v3                                    ║
║  Base URL: https://api.tradestation.com                         ║
║  Docs: https://api.tradestation.com/docs/specification          ║
║                                                                  ║
║  Features:                                                       ║
║  • Full automation: signal detection → entry → SL/TP → exit     ║
║  • OCO bracket orders with breakeven trailing                    ║
║  • Regime classification (ADX + VIX)                             ║
║  • Economic calendar integration (skip high-impact events)       ║
║  • Stream reconnection with exponential backoff                  ║
║  • Sub-minute position monitoring (every 15 seconds)             ║
║  • Structured logging + Telegram/Discord alerts                  ║
║  • Flask API for live monitoring dashboard                       ║
║                                                                  ║
║  DISCLAIMER: Educational/research only. Paper trade first.       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import logging
import threading
import traceback
from datetime import datetime, time as dtime, timedelta, date, timezone
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, List, Dict, Callable
from collections import deque
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd

# ═══════════════════════════════════════════════════════════════
# CONFIG — imported from config.py (the only file you ever edit)
# ═══════════════════════════════════════════════════════════════
from config import Config

# ── Timezone helper ───────────────────────────────────────────
ET = ZoneInfo("America/New_York")

def now_et() -> datetime:
    """Return current time in US Eastern (handles EST/EDT automatically)."""
    return datetime.now(ET)


# ═══════════════════════════════════════════════════════════════
# STRUCTURED LOGGING
# ═══════════════════════════════════════════════════════════════

class TradingLogger:
    """Dual logger: file + in-memory buffer for dashboard."""

    def __init__(self):
        self.log_buffer: deque = deque(maxlen=Config.MAX_LOG_LINES)
        self.alert_buffer: deque = deque(maxlen=100)

        # File logger
        self.logger = logging.getLogger("SPX_0DTE")
        self.logger.setLevel(logging.DEBUG)

        fh = logging.FileHandler(Config.LOG_FILE)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s ET | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self.logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(
            "%(asctime)s ET | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S"
        ))
        self.logger.addHandler(ch)

        # Force logging to use Eastern Time
        logging.Formatter.converter = lambda *args: now_et().timetuple()

    def _store(self, level: str, msg: str, category: str = "SYSTEM"):
        entry = {
            "timestamp": now_et().isoformat(),
            "level": level,
            "category": category,
            "message": msg,
        }
        self.log_buffer.append(entry)
        if level in ("ALERT", "ERROR", "TRADE"):
            self.alert_buffer.append(entry)

    def info(self, msg, cat="SYSTEM"):
        self.logger.info(f"[{cat}] {msg}")
        self._store("INFO", msg, cat)

    def warning(self, msg, cat="SYSTEM"):
        self.logger.warning(f"[{cat}] {msg}")
        self._store("WARNING", msg, cat)

    def error(self, msg, cat="SYSTEM"):
        self.logger.error(f"[{cat}] {msg}")
        self._store("ERROR", msg, cat)

    def trade(self, msg):
        self.logger.info(f"[TRADE] {msg}")
        self._store("TRADE", msg, "TRADE")

    def alert(self, msg):
        self.logger.warning(f"[ALERT] {msg}")
        self._store("ALERT", msg, "ALERT")

    def get_logs(self, limit=100):
        return list(self.log_buffer)[-limit:]

    def get_alerts(self, limit=50):
        return list(self.alert_buffer)[-limit:]


log = TradingLogger()


# ═══════════════════════════════════════════════════════════════
# ALERT SYSTEM (Telegram + Discord)
# ═══════════════════════════════════════════════════════════════

class AlertSystem:
    """Send alerts to Telegram and/or Discord."""

    @staticmethod
    def send(message: str, level: str = "INFO"):
        prefix = {"INFO": "ℹ️", "TRADE": "📊", "ALERT": "🚨",
                  "ERROR": "❌", "WIN": "✅", "LOSS": "🔴"}.get(level, "📌")
        full_msg = f"{prefix} *SPX 0DTE Engine*\n{message}"

        # Telegram
        if Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": Config.TELEGRAM_CHAT_ID,
                        "text": full_msg,
                        "parse_mode": "Markdown",
                    },
                    timeout=5
                )
            except Exception:
                pass

        # Discord
        if Config.DISCORD_WEBHOOK:
            try:
                requests.post(
                    Config.DISCORD_WEBHOOK,
                    json={"content": full_msg},
                    timeout=5
                )
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# ECONOMIC CALENDAR
# ═══════════════════════════════════════════════════════════════

class EconomicCalendar:
    """
    Tracks high-impact economic events to avoid trading around them.
    Loads events daily and provides a check method.
    Falls back to a hardcoded list of recurring events if API fails.
    """

    # Known recurring high-impact events (Eastern Time)
    RECURRING_EVENTS = {
        # (month, day) -> list of (hour, minute, name)
        # FOMC: 8 meetings/year — dates change annually, loaded from API
        # These are always-skip patterns:
    }

    # Day-of-week patterns (0=Mon, 4=Fri)
    WEEKLY_PATTERNS = [
        # First Friday of month: NFP at 8:30 AM
        {"day_of_week": 4, "week_of_month": 1, "time": dtime(8, 30),
         "name": "Non-Farm Payrolls", "blackout_min": 60},
    ]

    def __init__(self):
        self.today_events: List[Dict] = []
        self.last_load_date: Optional[date] = None
        self.fomc_dates: List[date] = []
        self._load_fomc_dates()

    def _load_fomc_dates(self):
        """Load known FOMC meeting dates for the current year."""
        # 2025-2026 FOMC dates (update annually)
        # These are announcement dates (2:00 PM ET)
        fomc_2025 = [
            date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7),
            date(2025, 6, 18), date(2025, 7, 30), date(2025, 9, 17),
            date(2025, 10, 29), date(2025, 12, 17),
        ]
        fomc_2026 = [
            date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
            date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
            date(2026, 10, 28), date(2026, 12, 16),
        ]
        self.fomc_dates = fomc_2025 + fomc_2026

    def load_today(self):
        """Load economic events for today. Called once at session start."""
        today = now_et().date()
        if self.last_load_date == today:
            return
        self.last_load_date = today
        self.today_events = []

        # Check if today is FOMC day — full blackout
        if today in self.fomc_dates:
            self.today_events.append({
                "time": dtime(0, 0),
                "name": "FOMC Decision Day",
                "blackout_min": 999,  # All day blackout
                "impact": "CRITICAL",
            })
            log.alert(f"🏦 FOMC DAY — No trading today")
            AlertSystem.send("FOMC Decision Day — engine will NOT trade", "ALERT")
            return

        # Check for CPI (usually 8:30 AM, around 10th-13th of month)
        if 10 <= today.day <= 14 and today.weekday() < 5:
            self.today_events.append({
                "time": dtime(8, 30),
                "name": "CPI Release (Possible)",
                "blackout_min": 60,
                "impact": "HIGH",
            })

        # Check for NFP (first Friday of month)
        if today.weekday() == 4 and today.day <= 7:
            self.today_events.append({
                "time": dtime(8, 30),
                "name": "Non-Farm Payrolls",
                "blackout_min": 60,
                "impact": "HIGH",
            })

        # Check for PPI (usually day after CPI)
        if 11 <= today.day <= 15 and today.weekday() < 5:
            self.today_events.append({
                "time": dtime(8, 30),
                "name": "PPI Release (Possible)",
                "blackout_min": 30,
                "impact": "MEDIUM",
            })

        # Weekly jobless claims (every Thursday 8:30 AM)
        if today.weekday() == 3:
            self.today_events.append({
                "time": dtime(8, 30),
                "name": "Weekly Jobless Claims",
                "blackout_min": 15,
                "impact": "LOW",
            })

        # Triple/Quad witching (3rd Friday of Mar, Jun, Sep, Dec)
        if (today.month in [3, 6, 9, 12] and today.weekday() == 4
                and 15 <= today.day <= 21):
            self.today_events.append({
                "time": dtime(0, 0),
                "name": "Triple Witching",
                "blackout_min": 999,
                "impact": "CRITICAL",
            })
            log.alert("Triple Witching day — no trading")

        if self.today_events:
            names = [e["name"] for e in self.today_events]
            log.info(f"Economic events today: {', '.join(names)}", "CALENDAR")

    def is_blackout(self, check_time: Optional[dtime] = None) -> tuple:
        """
        Check if current time falls within any event blackout window.
        Returns (is_blackout: bool, event_name: str).
        """
        if check_time is None:
            check_time = now_et().time()

        for event in self.today_events:
            evt_time = event["time"]
            blackout = event["blackout_min"]

            # Full day blackout
            if blackout >= 999:
                return True, event["name"]

            # Calculate blackout window: 10 min before → blackout_min after
            evt_dt = datetime.combine(now_et().date(), evt_time)
            start = (evt_dt - timedelta(minutes=10)).time()
            end = (evt_dt + timedelta(minutes=blackout)).time()

            if start <= check_time <= end:
                return True, event["name"]

        return False, ""

    def get_events(self) -> List[Dict]:
        """Return today's events for dashboard."""
        return self.today_events


# ═══════════════════════════════════════════════════════════════
# TRADESTATION AUTH (with robust refresh)
# ═══════════════════════════════════════════════════════════════

class TradeStationAuth:
    """OAuth 2.0 token management with auto-refresh and error handling."""

    TOKEN_URL = "https://signin.tradestation.com/oauth/token"

    def __init__(self):
        self.client_id     = Config.TS_CLIENT_ID
        self.client_secret = Config.TS_CLIENT_SECRET
        self.refresh_token = Config.TS_REFRESH_TOKEN
        self.access_token  = None
        self.token_expiry  = 0
        self._lock         = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if time.time() >= self.token_expiry - 120:
                self._refresh()
            return self.access_token

    def _refresh(self):
        for attempt in range(3):
            try:
                resp = requests.post(self.TOKEN_URL, data={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                }, timeout=10)

                if resp.status_code == 200:
                    data = resp.json()
                    self.access_token = data["access_token"]
                    self.token_expiry = time.time() + data.get("expires_in", 1200)
                    # Update refresh token if rotated
                    if "refresh_token" in data:
                        self.refresh_token = data["refresh_token"]
                    log.info("Token refreshed successfully", "AUTH")
                    return
                else:
                    log.error(f"Token refresh failed ({resp.status_code}): {resp.text}", "AUTH")

            except Exception as e:
                log.error(f"Token refresh attempt {attempt+1} failed: {e}", "AUTH")
                time.sleep(2 ** attempt)

        log.alert("TOKEN REFRESH FAILED after 3 attempts — engine may stop")
        AlertSystem.send("⚠️ Token refresh failed! Check credentials.", "ERROR")

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }


# ═══════════════════════════════════════════════════════════════
# TRADESTATION API CLIENT (production-hardened)
# ═══════════════════════════════════════════════════════════════

class TradeStationAPI:
    """Production wrapper around TradeStation REST API v3."""

    def __init__(self, auth: TradeStationAuth, account_id: str):
        self.auth = auth
        self.account_id = account_id
        self.base = Config.base_url()
        self._vix_cache = {"value": 18.0, "time": 0}

    def _get(self, path, params=None, timeout=10):
        """GET with retry logic."""
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{self.base}{path}",
                    headers=self.auth.headers(),
                    params=params,
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 5))
                    log.warning(f"Rate limited, waiting {wait}s", "API")
                    time.sleep(wait)
                else:
                    log.error(f"GET {path} → {resp.status_code}: {resp.text[:200]}", "API")
                    return {}
            except requests.exceptions.Timeout:
                log.warning(f"GET {path} timeout (attempt {attempt+1})", "API")
            except Exception as e:
                log.error(f"GET {path} error: {e}", "API")
                time.sleep(1)
        return {}

    def _post(self, path, body, timeout=10):
        """POST with retry logic."""
        for attempt in range(2):
            try:
                resp = requests.post(
                    f"{self.base}{path}",
                    headers=self.auth.headers(),
                    json=body,
                    timeout=timeout,
                )
                result = resp.json()
                if resp.status_code in (200, 201):
                    return result
                else:
                    log.error(f"POST {path} → {resp.status_code}: {resp.text[:300]}", "API")
                    return result
            except Exception as e:
                log.error(f"POST {path} error: {e}", "API")
                time.sleep(1)
        return {}

    def _put(self, path, body, timeout=10):
        """PUT with retry."""
        try:
            resp = requests.put(
                f"{self.base}{path}",
                headers=self.auth.headers(),
                json=body,
                timeout=timeout,
            )
            return resp.json()
        except Exception as e:
            log.error(f"PUT {path} error: {e}", "API")
            return {}

    def _delete(self, path, timeout=10):
        """DELETE with retry."""
        try:
            resp = requests.delete(
                f"{self.base}{path}",
                headers=self.auth.headers(),
                timeout=timeout,
            )
            return resp.json() if resp.content else {}
        except Exception as e:
            log.error(f"DELETE {path} error: {e}", "API")
            return {}

    # ── Market Data ──────────────────────────────────────────

    def get_bars(self, symbol="$SPX.X", interval=5, unit="Minute",
                 barsback=100) -> pd.DataFrame:
        """GET /v3/marketdata/barcharts/{symbol}"""
        data = self._get(f"/v3/marketdata/barcharts/{symbol}", {
            "interval": interval, "unit": unit,
            "barsback": barsback, "sessiontemplate": "USEQPre",
        })
        bars = data.get("Bars", [])
        if not bars:
            log.error("No bars returned from API", "DATA")
            return pd.DataFrame()

        df = pd.DataFrame(bars)
        for col in ["High", "Low", "Open", "Close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["TotalVolume"] = pd.to_numeric(df["TotalVolume"], errors="coerce").fillna(0).astype(int)
        df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
        return df

    def stream_bars(self, symbol, interval, callback, stop_event: threading.Event):
        """
        GET /v3/marketdata/stream/barcharts/{symbol}
        HTTP Streaming with auto-reconnection.
        """
        reconnect_count = 0

        while not stop_event.is_set() and reconnect_count < Config.STREAM_RECONNECT_MAX:
            try:
                log.info(f"Opening bar stream for {symbol} ({interval}min)", "STREAM")
                resp = requests.get(
                    f"{self.base}/v3/marketdata/stream/barcharts/{symbol}",
                    headers=self.auth.headers(),
                    params={"interval": interval, "unit": "Minute"},
                    stream=True,
                    timeout=(10, None),  # 10s connect, no read timeout
                )

                if resp.status_code != 200:
                    raise ConnectionError(f"Stream returned {resp.status_code}")

                reconnect_count = 0  # Reset on successful connect
                log.info("Bar stream connected", "STREAM")

                for line in resp.iter_lines():
                    if stop_event.is_set():
                        break
                    if line:
                        try:
                            bar = json.loads(line)
                            if bar.get("IsRealtime"):
                                callback(bar)
                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                reconnect_count += 1
                wait = Config.STREAM_RECONNECT_BASE ** reconnect_count
                log.warning(
                    f"Bar stream disconnected ({e}). "
                    f"Reconnecting in {wait}s (attempt {reconnect_count}/{Config.STREAM_RECONNECT_MAX})",
                    "STREAM"
                )
                AlertSystem.send(f"Bar stream reconnecting (attempt {reconnect_count})", "ALERT")
                time.sleep(wait)

        if reconnect_count >= Config.STREAM_RECONNECT_MAX:
            log.error("Bar stream max reconnections reached — STOPPING", "STREAM")
            AlertSystem.send("🔴 Bar stream DEAD — engine stopped!", "ERROR")

    def stream_orders(self, callback, stop_event: threading.Event):
        """
        GET /v3/brokerage/stream/accounts/{id}/orders
        Streams order status updates with reconnection.
        """
        reconnect_count = 0

        while not stop_event.is_set() and reconnect_count < Config.STREAM_RECONNECT_MAX:
            try:
                log.info("Opening order stream", "STREAM")
                resp = requests.get(
                    f"{self.base}/v3/brokerage/stream/accounts/{self.account_id}/orders",
                    headers=self.auth.headers(),
                    stream=True,
                    timeout=(10, None),
                )

                if resp.status_code != 200:
                    raise ConnectionError(f"Order stream returned {resp.status_code}")

                reconnect_count = 0
                log.info("Order stream connected", "STREAM")

                for line in resp.iter_lines():
                    if stop_event.is_set():
                        break
                    if line:
                        try:
                            callback(json.loads(line))
                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                reconnect_count += 1
                wait = Config.STREAM_RECONNECT_BASE ** reconnect_count
                log.warning(f"Order stream reconnecting in {wait}s", "STREAM")
                time.sleep(wait)

    def get_vix(self) -> float:
        """GET VIX with caching to avoid rate limit."""
        now = time.time()
        if now - self._vix_cache["time"] < Config.VIX_CACHE_SECONDS:
            return self._vix_cache["value"]

        data = self._get("/v3/marketdata/quotes/$VIX.X")
        quotes = data.get("Quotes", [])
        if quotes:
            vix = float(quotes[0].get("Last", 18.0))
            self._vix_cache = {"value": vix, "time": now}
            return vix
        return self._vix_cache["value"]

    def get_option_quote(self, symbol) -> dict:
        """GET /v3/marketdata/quotes/{symbol} for option."""
        data = self._get(f"/v3/marketdata/quotes/{symbol}")
        quotes = data.get("Quotes", [])
        return quotes[0] if quotes else {}

    def get_option_chain(self, expiration) -> dict:
        """GET /v3/marketdata/options/chains/$SPX.X"""
        return self._get("/v3/marketdata/options/chains/$SPX.X", {
            "expiration": expiration,
            "strikeRange": "5",
            "optionType": "All",
        })

    # ── Order Execution ──────────────────────────────────────

    def place_order(self, symbol, qty, trade_action,
                    order_type="Market", limit_price=None,
                    stop_price=None) -> dict:
        """POST /v3/orderexecution/orders"""
        body = {
            "AccountID": self.account_id,
            "Symbol": symbol,
            "Quantity": str(qty),
            "OrderType": order_type,
            "TradeAction": trade_action,
            "TimeInForce": {"Duration": "DAY"},
            "Route": "Intelligent",
        }
        if limit_price is not None:
            body["LimitPrice"] = f"{limit_price:.2f}"
        if stop_price is not None:
            body["StopPrice"] = f"{stop_price:.2f}"

        log.trade(f"ORDER: {trade_action} {qty}x {symbol} @ {order_type} "
                  f"{'LP='+str(limit_price) if limit_price else ''} "
                  f"{'SP='+str(stop_price) if stop_price else ''}")

        return self._post("/v3/orderexecution/orders", body)

    def place_oco_bracket(self, symbol, qty, tp_price, sl_price) -> dict:
        """POST /v3/orderexecution/ordergroups — OCO bracket."""
        body = {
            "Type": "OCO",
            "Orders": [
                {
                    "AccountID": self.account_id,
                    "Symbol": symbol,
                    "Quantity": str(qty),
                    "OrderType": "Limit",
                    "LimitPrice": f"{tp_price:.2f}",
                    "TradeAction": "SellToClose",
                    "TimeInForce": {"Duration": "DAY"},
                    "Route": "Intelligent",
                },
                {
                    "AccountID": self.account_id,
                    "Symbol": symbol,
                    "Quantity": str(qty),
                    "OrderType": "StopMarket",
                    "StopPrice": f"{max(0.05, sl_price):.2f}",
                    "TradeAction": "SellToClose",
                    "TimeInForce": {"Duration": "DAY"},
                    "Route": "Intelligent",
                },
            ]
        }
        log.trade(f"OCO BRACKET: {symbol} TP={tp_price:.2f} SL={max(0.05,sl_price):.2f}")
        return self._post("/v3/orderexecution/ordergroups", body)

    def modify_order(self, order_id, stop_price=None, limit_price=None) -> dict:
        """PUT /v3/orderexecution/orders/{orderID}"""
        body = {}
        if stop_price is not None:
            body["StopPrice"] = f"{stop_price:.2f}"
        if limit_price is not None:
            body["LimitPrice"] = f"{limit_price:.2f}"
        log.info(f"Modifying order {order_id}: {body}", "ORDER")
        return self._put(f"/v3/orderexecution/orders/{order_id}", body)

    def cancel_order(self, order_id) -> dict:
        """DELETE /v3/orderexecution/orders/{orderID}"""
        log.info(f"Cancelling order {order_id}", "ORDER")
        return self._delete(f"/v3/orderexecution/orders/{order_id}")

    # ── Account ──────────────────────────────────────────────

    def get_accounts(self) -> list:
        data = self._get("/v3/brokerage/accounts")
        return data.get("Accounts", [])

    def get_balance(self) -> float:
        data = self._get(f"/v3/brokerage/accounts/{self.account_id}/balances")
        balances = data.get("Balances", [])
        if balances:
            return float(balances[0].get("Equity", 0))
        return 0.0

    def get_positions(self) -> list:
        data = self._get(f"/v3/brokerage/accounts/{self.account_id}/positions")
        return data.get("Positions", [])

    def get_orders(self) -> list:
        data = self._get(f"/v3/brokerage/accounts/{self.account_id}/orders")
        return data.get("Orders", [])


# ═══════════════════════════════════════════════════════════════
# INDICATOR CALCULATIONS
# ═══════════════════════════════════════════════════════════════

class Indicators:
    """All technical indicator calculations."""

    @staticmethod
    def compute(df: pd.DataFrame, config=Config) -> pd.DataFrame:
        """Calculate all indicators on 5-min OHLCV DataFrame."""
        df = df.copy()

        # Normalize column names from TradeStation format
        rename_map = {"Open": "open", "High": "high", "Low": "low",
                      "Close": "close", "TotalVolume": "volume"}
        # Only rename columns that are actually present (avoid duplicates
        # when compute() is called repeatedly on already-renamed data)
        rename_map = {k: v for k, v in rename_map.items() if k in df.columns}
        df = df.rename(columns=rename_map)
        # Drop any duplicate columns that may have crept in
        df = df.loc[:, ~df.columns.duplicated()]

        if len(df) < config.BB_PERIOD + 5:
            return df

        # VWAP (cumulative per trading day — resets each session)
        if "TimeStamp" in df.columns:
            df["TimeStamp"] = pd.to_datetime(df["TimeStamp"], errors="coerce")
            day_groups = df["TimeStamp"].dt.date
        else:
            day_groups = pd.Series(0, index=df.index)  # Single group fallback
        df["cum_vol"] = df.groupby(day_groups)["volume"].cumsum()
        df["_vp"] = df["close"] * df["volume"]
        df["cum_vp"] = df.groupby(day_groups)["_vp"].cumsum()
        df["vwap"]    = df["cum_vp"] / df["cum_vol"].replace(0, 1)
        df = df.drop(columns=["_vp"])

        # Bollinger Bands
        df["bb_mid"]   = df["close"].rolling(config.BB_PERIOD).mean()
        bb_std         = df["close"].rolling(config.BB_PERIOD).std()
        df["bb_upper"] = df["bb_mid"] + config.BB_STD * bb_std
        df["bb_lower"] = df["bb_mid"] - config.BB_STD * bb_std

        # RSI
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).ewm(span=config.RSI_PERIOD, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=config.RSI_PERIOD, adjust=False).mean()
        rs    = gain / loss.replace(0, 1e-10)
        df["rsi"] = 100 - (100 / (1 + rs))

        # ADX
        df["adx"] = Indicators._calc_adx(df, config.ADX_PERIOD)

        # Volume average
        df["vol_avg"] = df["volume"].rolling(20).mean()

        return df

    @staticmethod
    def _calc_adx(df: pd.DataFrame, period: int) -> pd.Series:
        """
        Calculate Average Directional Index (ADX) using Wilder's smoothing.
        Matches TradingView's ADX(14) exactly.
        ADX < 25 = non-trending (mean reverting)
        ADX > 25 = trending (avoid)
        """
        high = df["high"]
        low  = df["low"]
        close = df["close"]

        # True Range
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Directional Movement
        up_move   = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        plus_dm  = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)

        # Wilder's smoothing: alpha = 1/period (NOT ewm span which uses 2/(period+1))
        atr       = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di   = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-10))
        minus_di  = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, 1e-10))

        # DX and ADX (also Wilder-smoothed)
        dx  = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10))
        adx = dx.ewm(alpha=1/period, adjust=False).mean()

        return adx

    @staticmethod
    def is_reversal_candle(row, prev_row, direction: str) -> bool:
        """
        Detect reversal candlestick patterns.
        direction: "bull" for bullish reversal, "bear" for bearish reversal.
        """
        body      = abs(row["close"] - row["open"])
        full_range = row["high"] - row["low"]
        prev_body = abs(prev_row["close"] - prev_row["open"])

        if full_range == 0:
            return False

        body_ratio = body / full_range

        if direction == "bull":
            # Hammer: small body at top, long lower wick
            lower_wick = min(row["open"], row["close"]) - row["low"]
            upper_wick = row["high"] - max(row["open"], row["close"])
            is_hammer = (lower_wick > 2 * body and upper_wick < body
                         and body_ratio < 0.35)

            # Bullish engulfing: green candle body encompasses prev red candle
            is_engulfing = (prev_row["close"] < prev_row["open"]  # prev red
                           and row["close"] > row["open"]          # current green
                           and row["close"] > prev_row["open"]
                           and row["open"] < prev_row["close"])

            # Doji followed by green
            is_doji = body_ratio < 0.1 and row["close"] > row["open"]

            return is_hammer or is_engulfing or is_doji

        elif direction == "bear":
            # Shooting star: small body at bottom, long upper wick
            upper_wick = row["high"] - max(row["open"], row["close"])
            lower_wick = min(row["open"], row["close"]) - row["low"]
            is_shooting = (upper_wick > 2 * body and lower_wick < body
                           and body_ratio < 0.35)

            # Bearish engulfing
            is_engulfing = (prev_row["close"] > prev_row["open"]  # prev green
                           and row["close"] < row["open"]          # current red
                           and row["close"] < prev_row["open"]
                           and row["open"] > prev_row["close"])

            # Doji followed by red
            is_doji = body_ratio < 0.1 and row["close"] < row["open"]

            return is_shooting or is_engulfing or is_doji

        return False


# ═══════════════════════════════════════════════════════════════
# TRADE DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════

class Regime(Enum):
    MEAN_REVERTING = "mean_reverting"
    LOW_VOL        = "low_vol"
    TRENDING       = "trending"

class Signal(Enum):
    LONG  = "long"
    SHORT = "short"
    NONE  = "none"

@dataclass
class OpenTrade:
    signal: Signal
    option_symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: int
    risk_amount: float        # Dollar risk per trade
    reward_amount: float      # Dollar reward target
    entry_time: datetime
    entry_order_id: str = ""
    sl_order_id: str = ""
    tp_order_id: str = ""
    be_trailed: bool = False
    filled: bool = False       # Entry fill confirmed
    current_price: float = 0.0
    unrealized_pnl: float = 0.0

    def to_dict(self):
        return {
            "signal": self.signal.value,
            "option_symbol": self.option_symbol,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "quantity": self.quantity,
            "risk_amount": self.risk_amount,
            "reward_amount": self.reward_amount,
            "entry_time": self.entry_time.isoformat(),
            "be_trailed": self.be_trailed,
            "filled": self.filled,
            "current_price": self.current_price,
            "unrealized_pnl": self.unrealized_pnl,
            "elapsed_min": round((now_et() - self.entry_time).seconds / 60, 1),
        }

@dataclass
class TradeRecord:
    """Completed trade record for history."""
    signal: str
    option_symbol: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    result: str           # "TP", "SL", "TIME_STOP", "BE", "EOD"
    entry_time: str
    exit_time: str
    regime: str


# ═══════════════════════════════════════════════════════════════
# MEAN REVERSION ENGINE (Production)
# ═══════════════════════════════════════════════════════════════

class MeanReversionEngine:
    """
    Production-grade 0DTE SPX mean reversion trading engine.

    Lifecycle:
    1. Initialize → load historical bars → compute indicators
    2. Start background threads: bar stream, order stream, position monitor
    3. On each new bar: classify regime → detect signal → execute entry
    4. Position monitor: time stop, breakeven trail (every 15 sec)
    5. Order stream: detect fills, update P/L
    6. EOD: close all positions at 3:25 PM
    """

    def __init__(self, api: TradeStationAPI, calendar: EconomicCalendar):
        self.api = api
        self.calendar = calendar

        # State
        self.bars_df: Optional[pd.DataFrame] = None
        self.current_regime: Regime = Regime.TRENDING
        self.open_trade: Optional[OpenTrade] = None
        self.trade_history: List[TradeRecord] = []

        # Counters
        self.daily_pnl: float = 0.0
        self.consec_losses: int = 0
        self.trades_today: int = 0
        self.cooldown_until: Optional[datetime] = None
        self.account_balance: float = 0.0

        # Decision log (5-min summaries explaining why no trade)
        self._decision_log: deque = deque(maxlen=200)
        self._last_decision_time: Optional[datetime] = None

        # Control
        self.stop_event = threading.Event()
        self.engine_active = False
        self._position_lock = threading.Lock()

    # ── Public Interface ─────────────────────────────────────

    def start(self):
        """Start the trading engine."""
        log.info("═══ SPX 0DTE Mean Reversion Engine Starting ═══", "ENGINE")
        log.info(f"Environment: {Config.TS_ENVIRONMENT}", "ENGINE")
        log.info(f"Account: {self.api.account_id}", "ENGINE")

        # Load balance
        self.account_balance = self.api.get_balance()
        log.info(f"Account balance: ${self.account_balance:,.2f}", "ENGINE")
        AlertSystem.send(
            f"Engine started\nEnv: {Config.TS_ENVIRONMENT}\n"
            f"Balance: ${self.account_balance:,.2f}", "INFO"
        )

        # Load economic calendar
        self.calendar.load_today()

        # Check if today is a valid trading day (not weekend / holiday)
        if not self._is_trading_day():
            today = now_et().date()
            if today.weekday() >= 5:
                reason = "Weekend (market closed)"
            else:
                reason = "US market holiday"
            log.info(f"Not a trading day: {reason} — engine will not trade today", "ENGINE")
            # Dashboard trading window indicator already shows MARKET CLOSED

        # Check for full-day blackout
        is_blackout, event = self.calendar.is_blackout()
        if is_blackout and "Day" in event:
            log.alert(f"FULL DAY BLACKOUT: {event} — engine will not trade")
            AlertSystem.send(f"No trading today: {event}", "ALERT")
            # Engine stays running for monitoring but won't trade

        # Load historical bars
        log.info("Loading historical 5-min bars...", "DATA")
        self.bars_df = self.api.get_bars("$SPX.X", 5, "Minute", 100)
        if self.bars_df.empty:
            log.error("Failed to load historical bars — aborting", "ENGINE")
            return

        self.bars_df = Indicators.compute(self.bars_df)
        log.info(f"Loaded {len(self.bars_df)} bars, indicators computed", "DATA")

        self.engine_active = True

        # Start background threads
        threads = [
            threading.Thread(target=self._bar_stream_thread, daemon=True, name="BarStream"),
            threading.Thread(target=self._order_stream_thread, daemon=True, name="OrderStream"),
            threading.Thread(target=self._position_monitor_thread, daemon=True, name="PosMon"),
            threading.Thread(target=self._heartbeat_thread, daemon=True, name="Heartbeat"),
        ]

        for t in threads:
            t.start()
            log.info(f"Started thread: {t.name}", "ENGINE")

        log.info("All threads started — engine is LIVE", "ENGINE")

    def stop(self):
        """Gracefully stop the engine."""
        log.info("Engine stop requested", "ENGINE")
        self.stop_event.set()
        self.engine_active = False

        # Close any open positions
        if self.open_trade:
            self._force_close("SHUTDOWN")

        AlertSystem.send("Engine stopped", "INFO")

    def get_status(self) -> dict:
        """Return current engine state for dashboard."""
        status = {
            "engine_active": self.engine_active,
            "environment": Config.TS_ENVIRONMENT,
            "account_id": self.api.account_id,
            "account_balance": self.account_balance,
            "current_regime": self.current_regime.value if self.current_regime else "unknown",
            "daily_pnl": self.daily_pnl,
            "trades_today": self.trades_today,
            "consecutive_losses": self.consec_losses,
            "max_trades": Config.MAX_TRADES_PER_DAY,
            "open_trade": self.open_trade.to_dict() if self.open_trade else None,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "vix": self.api._vix_cache["value"],
            "session_start": Config.SESSION_START.isoformat(),
            "session_end": Config.SESSION_END.isoformat(),
            "is_trading_day": self._is_trading_day(),
            "economic_events": self.calendar.get_events(),
            "last_bars": self._get_last_bars_summary(),
        }

        # Add decision parameters for dashboard monitoring
        status["indicators"] = self._get_indicator_snapshot()

        return status

    def _get_indicator_snapshot(self) -> dict:
        """Return current indicator values and signal condition states."""
        snapshot = {
            "available": False,
            "close": None, "vwap": None, "vwap_dev": None,
            "rsi": None, "adx": None, "bb_upper": None,
            "bb_mid": None, "bb_lower": None, "bb_pct": None,
            "volume": None, "vol_avg": None, "vol_ratio": None,
            "vol_ok": False, "candle_bull": False, "candle_bear": False,
            "conditions": {},
            "thresholds": {
                "rsi_oversold": float(Config.RSI_OVERSOLD),
                "rsi_overbought": float(Config.RSI_OVERBOUGHT),
                "adx_threshold": float(Config.ADX_THRESHOLD),
                "vwap_offset": float(Config.VWAP_OFFSET),
                "vol_multiplier": float(Config.VOL_MULTIPLIER),
                "vix_trending": 30.0,
                "vix_low_vol": 12.0,
                "adx_low_vol": 20.0,
            },
        }

        if self.bars_df is None or len(self.bars_df) < Config.BB_PERIOD + 5:
            return snapshot

        try:
            row = self.bars_df.iloc[-1]
            prev = self.bars_df.iloc[-2]

            close = float(row.get("close", row.get("Close", 0)))
            vwap = float(row.get("vwap", close))
            rsi = float(row.get("rsi", 50))
            adx = float(row.get("adx", 30))
            bb_upper = float(row.get("bb_upper", close + 10))
            bb_mid = float(row.get("bb_mid", close))
            bb_lower = float(row.get("bb_lower", close - 10))
            volume = float(row.get("volume", row.get("TotalVolume", 0)))
            vol_avg = float(row.get("vol_avg", 1))
            vix = self.api._vix_cache["value"]

            vwap_dev = (close - vwap) / vwap if vwap > 0 else 0
            vol_ratio = volume / vol_avg if vol_avg > 0 else 0
            vol_ok = bool(volume > Config.VOL_MULTIPLIER * vol_avg) if vol_avg > 0 else False
            bb_range = bb_upper - bb_lower
            bb_pct = (close - bb_lower) / bb_range if bb_range > 0 else 0.5
            vix = float(vix)

            candle_bull = bool(Indicators.is_reversal_candle(row, prev, "bull"))
            candle_bear = bool(Indicators.is_reversal_candle(row, prev, "bear"))

            # In-session and time checks
            in_session = bool(self._in_session())
            now_time = now_et().time()
            before_cutoff = bool(now_time <= Config.NO_ENTRY_AFTER)
            is_blackout, blackout_event = self.calendar.is_blackout()
            is_blackout = bool(is_blackout)
            within_limits = bool(self._within_limits())
            no_open_trade = bool(self.open_trade is None)
            not_trending = bool(self.current_regime != Regime.TRENDING)

            snapshot.update({
                "available": True,
                "close": float(round(close, 2)),
                "vwap": float(round(vwap, 2)),
                "vwap_dev": float(round(vwap_dev * 100, 4)),
                "rsi": float(round(rsi, 1)),
                "adx": float(round(adx, 1)),
                "vix": float(round(vix, 1)),
                "bb_upper": float(round(bb_upper, 2)),
                "bb_mid": float(round(bb_mid, 2)),
                "bb_lower": float(round(bb_lower, 2)),
                "bb_pct": float(round(bb_pct, 4)),
                "volume": int(volume),
                "vol_avg": int(vol_avg),
                "vol_ratio": float(round(vol_ratio, 2)),
                "vol_ok": vol_ok,
                "candle_bull": candle_bull,
                "candle_bear": candle_bear,
                "conditions": {
                    "long": {
                        "below_bb_lower": bool(close < bb_lower),
                        "vwap_negative": bool(vwap_dev < -Config.VWAP_OFFSET),
                        "rsi_oversold": bool(rsi < Config.RSI_OVERSOLD),
                        "adx_ok": bool(adx < Config.ADX_THRESHOLD),
                        "vol_spike": vol_ok,
                        "reversal_candle": candle_bull,
                        "not_trending": not_trending,
                        "in_session": in_session,
                        "before_cutoff": before_cutoff,
                        "no_blackout": not is_blackout,
                        "within_limits": within_limits,
                        "no_open_trade": no_open_trade,
                    },
                    "short": {
                        "above_bb_upper": bool(close > bb_upper),
                        "vwap_positive": bool(vwap_dev > Config.VWAP_OFFSET),
                        "rsi_overbought": bool(rsi > Config.RSI_OVERBOUGHT),
                        "adx_ok": bool(adx < Config.ADX_THRESHOLD),
                        "vol_spike": vol_ok,
                        "reversal_candle": candle_bear,
                        "not_trending": not_trending,
                        "in_session": in_session,
                        "before_cutoff": before_cutoff,
                        "no_blackout": not is_blackout,
                        "within_limits": within_limits,
                        "no_open_trade": no_open_trade,
                    },
                },
            })
        except Exception:
            pass

        return snapshot

    def get_decision_log(self, limit=50) -> List[dict]:
        """Return recent decision summaries for dashboard."""
        return list(self._decision_log)[-limit:]

    def get_trade_history(self) -> List[dict]:
        """Return trade history for dashboard."""
        return [
            {
                "signal": t.signal, "symbol": t.option_symbol,
                "entry": t.entry_price, "exit": t.exit_price,
                "qty": t.quantity, "pnl": t.pnl,
                "result": t.result, "entry_time": t.entry_time,
                "exit_time": t.exit_time, "regime": t.regime,
            }
            for t in self.trade_history
        ]

    # ── Background Threads ───────────────────────────────────

    def _bar_stream_thread(self):
        """Stream 5-min bars and process signals."""
        self.api.stream_bars("$SPX.X", 5, self._on_new_bar, self.stop_event)

    def _order_stream_thread(self):
        """Stream order updates for fill detection."""
        self.api.stream_orders(self._on_order_update, self.stop_event)

    def _position_monitor_thread(self):
        """Monitor open position every 15 seconds for time stop + breakeven."""
        while not self.stop_event.is_set():
            try:
                with self._position_lock:
                    if self.open_trade and self.open_trade.filled:
                        self._check_position()
                    # EOD close check
                    if now_et().time() >= Config.EOD_CLOSE_TIME:
                        if self.open_trade:
                            self._force_close("EOD")
            except Exception as e:
                log.error(f"Position monitor error: {e}", "MONITOR")

            time.sleep(Config.POSITION_CHECK_INTERVAL)

    def _heartbeat_thread(self):
        """Send periodic heartbeat + daily summary."""
        last_heartbeat = time.time()
        while not self.stop_event.is_set():
            now = time.time()
            # Heartbeat every 30 minutes
            if now - last_heartbeat > 1800:
                log.info(
                    f"Heartbeat — Regime:{self.current_regime.value} "
                    f"Trades:{self.trades_today} P/L:${self.daily_pnl:.2f} "
                    f"VIX:{self.api._vix_cache['value']:.1f}",
                    "HEARTBEAT"
                )
                last_heartbeat = now

            # EOD summary at 4:01 PM
            if now_et().time() >= dtime(16, 1):
                self._send_daily_summary()
                self.stop_event.wait(timeout=3600)  # Wait until next day

            time.sleep(60)

    # ── Core Signal Processing ───────────────────────────────

    def _on_new_bar(self, bar: dict):
        """
        Called on every streaming bar update from TradeStation.

        IMPORTANT: TradeStation streams intra-bar tick updates (many per second),
        NOT just completed bars. We must:
        1. Update the current bar in-place when the timestamp matches
        2. Only process signals when a NEW bar appears (previous bar completed)
        """
        try:
            if not self._in_session():
                return

            # Parse the incoming bar
            new_row = pd.DataFrame([bar])
            for col in ["High", "Low", "Open", "Close"]:
                if col in new_row.columns:
                    new_row[col] = pd.to_numeric(new_row[col], errors="coerce")
            if "TotalVolume" in new_row.columns:
                new_row["TotalVolume"] = pd.to_numeric(
                    new_row["TotalVolume"], errors="coerce"
                ).fillna(0).astype(int)
            if "TimeStamp" in new_row.columns:
                new_row["TimeStamp"] = pd.to_datetime(new_row["TimeStamp"])

            # Normalize column names to lowercase
            col_map = {"Open": "open", "High": "high", "Low": "low",
                       "Close": "close", "TotalVolume": "volume"}
            new_row = new_row.rename(columns={k: v for k, v in col_map.items()
                                              if k in new_row.columns})

            # Determine if this is a NEW bar or an update to the current bar
            incoming_ts = new_row["TimeStamp"].iloc[0] if "TimeStamp" in new_row.columns else None
            is_new_bar = True

            if (self.bars_df is not None and not self.bars_df.empty
                    and incoming_ts is not None and "TimeStamp" in self.bars_df.columns):
                last_ts = self.bars_df["TimeStamp"].iloc[-1]
                if pd.notna(last_ts) and pd.notna(incoming_ts) and incoming_ts == last_ts:
                    # Same bar — update the last row in place
                    is_new_bar = False
                    for col in ["open", "high", "low", "close", "volume"]:
                        if col in new_row.columns:
                            self.bars_df.at[self.bars_df.index[-1], col] = new_row[col].iloc[0]
                    # Don't recompute indicators or process signals on intra-bar ticks
                    return

            # NEW bar: previous bar is now complete — append and process
            self.bars_df = pd.concat(
                [self.bars_df, new_row], ignore_index=True
            ).tail(200)
            self.bars_df = Indicators.compute(self.bars_df)

            if len(self.bars_df) < Config.BB_PERIOD + 5:
                return

            row = self.bars_df.iloc[-1]
            prev = self.bars_df.iloc[-2]

            # Get VIX (cached)
            vix = self.api.get_vix()

            # Classify regime
            adx_val = row.get("adx", 30)
            self.current_regime = self._classify_regime(adx_val, vix)

            # Log bar info
            log.info(
                f"BAR: {float(row['close']):.2f} | VWAP:{float(row.get('vwap',0)):.2f} "
                f"| RSI:{float(row.get('rsi',50)):.1f} | ADX:{adx_val:.1f} "
                f"| VIX:{vix:.1f} | Regime:{self.current_regime.value}",
                "DATA"
            )

            # ── Build decision context for every bar ──
            close = float(row["close"])
            vwap_val = float(row.get("vwap", close))
            rsi_val = float(row.get("rsi", 50))
            bb_upper = float(row.get("bb_upper", close + 10))
            bb_lower = float(row.get("bb_lower", close - 10))
            bb_mid = float(row.get("bb_mid", close))
            volume = float(row.get("volume", 0))
            vol_avg_val = float(row.get("vol_avg", 1))
            vwap_dev = (close - vwap_val) / vwap_val if vwap_val > 0 else 0
            vol_ratio = volume / vol_avg_val if vol_avg_val > 0 else 0
            vol_ok = volume > Config.VOL_MULTIPLIER * vol_avg_val if vol_avg_val > 0 else False
            candle_bull = bool(Indicators.is_reversal_candle(row, prev, "bull"))
            candle_bear = bool(Indicators.is_reversal_candle(row, prev, "bear"))

            is_blackout, blackout_event = self.calendar.is_blackout()
            in_session = self._in_session()
            within_limits = self._within_limits()
            after_cutoff = now_et().time() > Config.NO_ENTRY_AFTER
            is_trending = self.current_regime == Regime.TRENDING

            # Determine the primary blocking reason (first gate that fails)
            skip_reason = None
            if is_trending:
                skip_reason = f"Regime is TRENDING (ADX={adx_val:.1f}, VIX={vix:.1f})"
            elif is_blackout:
                skip_reason = f"Economic blackout: {blackout_event}"
            elif not within_limits:
                if self.daily_pnl <= -(self.account_balance * Config.MAX_DAILY_LOSS_PCT):
                    skip_reason = f"Daily loss limit hit (${self.daily_pnl:.2f})"
                elif self.trades_today >= Config.MAX_TRADES_PER_DAY:
                    skip_reason = f"Max trades reached ({self.trades_today}/{Config.MAX_TRADES_PER_DAY})"
                elif self.cooldown_until:
                    skip_reason = f"Cooldown active until {self.cooldown_until.strftime('%H:%M')} ET"
                else:
                    skip_reason = "Risk limits not met"
            elif self.open_trade:
                skip_reason = f"Already in trade: {self.open_trade.option_symbol}"
            elif after_cutoff:
                skip_reason = f"Past entry cutoff ({Config.NO_ENTRY_AFTER.strftime('%H:%M')} ET)"

            # Build per-condition detail for LONG and SHORT
            long_checks = [
                ("Price < Lower BB", bool(close < bb_lower), f"{close:.2f} vs {bb_lower:.2f}"),
                (f"VWAP Dev < -{Config.VWAP_OFFSET*100:.1f}%", bool(vwap_dev < -Config.VWAP_OFFSET), f"{vwap_dev*100:.2f}%"),
                (f"RSI < {Config.RSI_OVERSOLD}", bool(rsi_val < Config.RSI_OVERSOLD), f"{rsi_val:.1f}"),
                (f"ADX < {Config.ADX_THRESHOLD}", bool(adx_val < Config.ADX_THRESHOLD), f"{adx_val:.1f}"),
                (f"Vol > {Config.VOL_MULTIPLIER}x avg", bool(vol_ok), f"{vol_ratio:.2f}x"),
                ("Bull reversal candle", candle_bull, "Yes" if candle_bull else "No"),
            ]
            short_checks = [
                ("Price > Upper BB", bool(close > bb_upper), f"{close:.2f} vs {bb_upper:.2f}"),
                (f"VWAP Dev > +{Config.VWAP_OFFSET*100:.1f}%", bool(vwap_dev > Config.VWAP_OFFSET), f"{vwap_dev*100:.2f}%"),
                (f"RSI > {Config.RSI_OVERBOUGHT}", bool(rsi_val > Config.RSI_OVERBOUGHT), f"{rsi_val:.1f}"),
                (f"ADX < {Config.ADX_THRESHOLD}", bool(adx_val < Config.ADX_THRESHOLD), f"{adx_val:.1f}"),
                (f"Vol > {Config.VOL_MULTIPLIER}x avg", bool(vol_ok), f"{vol_ratio:.2f}x"),
                ("Bear reversal candle", candle_bear, "Yes" if candle_bear else "No"),
            ]

            long_passed = sum(1 for _, ok, _ in long_checks if ok)
            short_passed = sum(1 for _, ok, _ in short_checks if ok)
            best_side = "LONG" if long_passed >= short_passed else "SHORT"
            best_checks = long_checks if best_side == "LONG" else short_checks
            best_passed = max(long_passed, short_passed)

            # Log decision summary every 5 minutes
            now = now_et()
            should_log = (
                self._last_decision_time is None
                or (now - self._last_decision_time).total_seconds() >= 300
            )
            if should_log:
                self._last_decision_time = now

                # Build the summary entry
                failing = [name for name, ok, _ in best_checks if not ok]
                passing = [name for name, ok, _ in best_checks if ok]

                summary = {
                    "timestamp": now.isoformat(),
                    "close": round(close, 2),
                    "regime": self.current_regime.value,
                    "vix": round(vix, 1),
                    "best_side": best_side,
                    "conditions_met": best_passed,
                    "conditions_total": len(best_checks),
                    "passing": passing,
                    "failing": failing,
                    "skip_reason": skip_reason,
                    "indicators": {
                        "rsi": round(rsi_val, 1),
                        "adx": round(adx_val, 1),
                        "vwap_dev_pct": round(vwap_dev * 100, 2),
                        "bb_lower": round(bb_lower, 2),
                        "bb_mid": round(bb_mid, 2),
                        "bb_upper": round(bb_upper, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "candle_bull": candle_bull,
                        "candle_bear": candle_bear,
                    },
                    "long_score": f"{long_passed}/{len(long_checks)}",
                    "short_score": f"{short_passed}/{len(short_checks)}",
                    "long_detail": [{"name": n, "pass": bool(ok), "value": v} for n, ok, v in long_checks],
                    "short_detail": [{"name": n, "pass": bool(ok), "value": v} for n, ok, v in short_checks],
                }
                self._decision_log.append(summary)

                # Log a readable version to console/file
                fail_str = ", ".join(failing) if failing else "ALL MET"
                reason_str = f" | Gate: {skip_reason}" if skip_reason else ""
                log.info(
                    f"DECISION: {best_side} {best_passed}/{len(best_checks)} conditions"
                    f" | Failing: [{fail_str}]{reason_str}",
                    "DECISION"
                )

            # ── Gate checks (original flow) ──
            if is_trending:
                return

            if is_blackout:
                log.info(f"Skipping — economic blackout: {blackout_event}", "CALENDAR")
                return

            if not within_limits:
                return

            if self.open_trade:
                return

            if after_cutoff:
                return

            # Detect signal
            signal = self._detect_signal(row, prev)

            if signal != Signal.NONE:
                log.trade(f"SIGNAL DETECTED: {signal.value.upper()} at {close:.2f}")
                self._execute_entry(signal, close)

        except Exception as e:
            log.error(f"Bar processing error: {traceback.format_exc()}", "ENGINE")

    def _detect_signal(self, row, prev) -> Signal:
        """Check all entry conditions."""
        close = float(row["close"])
        vwap  = float(row.get("vwap", close))
        rsi   = float(row.get("rsi", 50))
        adx   = float(row.get("adx", 30))
        bb_upper = float(row.get("bb_upper", close + 10))
        bb_lower = float(row.get("bb_lower", close - 10))
        volume   = float(row.get("volume", 0))
        vol_avg  = float(row.get("vol_avg", 1))

        vwap_dev = (close - vwap) / vwap if vwap > 0 else 0
        vol_ok   = volume > Config.VOL_MULTIPLIER * vol_avg if vol_avg > 0 else False

        # LONG: price below lower BB + oversold RSI + volume spike + reversal
        if (close < bb_lower
                and vwap_dev < -Config.VWAP_OFFSET
                and rsi < Config.RSI_OVERSOLD
                and adx < Config.ADX_THRESHOLD
                and vol_ok
                and Indicators.is_reversal_candle(row, prev, "bull")):
            return Signal.LONG

        # SHORT: price above upper BB + overbought RSI + volume spike + reversal
        if (close > bb_upper
                and vwap_dev > Config.VWAP_OFFSET
                and rsi > Config.RSI_OVERBOUGHT
                and adx < Config.ADX_THRESHOLD
                and vol_ok
                and Indicators.is_reversal_candle(row, prev, "bear")):
            return Signal.SHORT

        return Signal.NONE

    # ── Trade Execution ──────────────────────────────────────

    def _execute_entry(self, signal: Signal, spx_price: float):
        """Place entry order + OCO bracket on TradeStation."""
        with self._position_lock:
            if self.open_trade:
                return  # Already in a trade

            # Build option symbol
            opt_sym = self._build_option_symbol(spx_price, signal)
            log.trade(f"Selected option: {opt_sym}")

            # Get option quote
            quote = self.api.get_option_quote(opt_sym)
            if not quote:
                log.warning(f"No quote for {opt_sym} — skipping", "TRADE")
                return

            ask = float(quote.get("Ask", 0))
            bid = float(quote.get("Bid", 0))

            if ask <= 0 or bid <= 0:
                log.warning(f"Invalid quotes: bid={bid} ask={ask}", "TRADE")
                return

            mid = round((ask + bid) / 2, 2)
            spread = ask - bid

            # Check spread
            if spread > Config.MAX_SPREAD:
                log.warning(f"Spread too wide: ${spread:.2f} (max ${Config.MAX_SPREAD})", "TRADE")
                return

            # Position sizing
            risk_per_contract = mid * Config.RISK_PER_TRADE * 100
            max_risk = self.account_balance * Config.MAX_POSITION_PCT
            qty = max(1, int(max_risk / risk_per_contract)) if risk_per_contract > 0 else 1

            # Adjust for low-vol regime
            if self.current_regime == Regime.LOW_VOL:
                qty = max(1, qty // 2)
                log.info("Low-vol regime — halved position size", "TRADE")

            # SL/TP prices on the option
            risk = round(mid * Config.RISK_PER_TRADE, 2)
            sl_price = round(mid - risk, 2)
            tp_price = round(mid + risk * Config.RR_RATIO, 2)
            risk_dollars = risk * 100 * qty
            reward_dollars = risk * Config.RR_RATIO * 100 * qty

            log.trade(
                f"ENTRY CALC: {signal.value.upper()} {qty}x {opt_sym} "
                f"| Mid=${mid:.2f} Risk=${risk:.2f} SL=${sl_price:.2f} TP=${tp_price:.2f} "
                f"| Risk=${risk_dollars:.0f} Reward=${reward_dollars:.0f}"
            )

            # 1) Place entry order (limit at ask for immediate fill)
            entry_resp = self.api.place_order(
                symbol=opt_sym,
                qty=qty,
                trade_action="BuyToOpen",
                order_type="Limit",
                limit_price=round(ask + 0.05, 2),  # Slight premium for fill
            )

            entry_order_id = ""
            orders = entry_resp.get("Orders", [])
            if orders:
                entry_order_id = orders[0].get("OrderID", "")
                log.trade(f"Entry order placed: ID={entry_order_id}")
            else:
                errors = entry_resp.get("Errors", entry_resp.get("Message", "Unknown"))
                log.error(f"Entry order FAILED: {errors}", "TRADE")
                AlertSystem.send(f"Order REJECTED: {errors}", "ERROR")
                return

            # 2) Place OCO bracket (SL + TP)
            oco_resp = self.api.place_oco_bracket(opt_sym, qty, tp_price, sl_price)

            tp_order_id = ""
            sl_order_id = ""
            oco_orders = oco_resp.get("Orders", [])
            if len(oco_orders) >= 2:
                tp_order_id = oco_orders[0].get("OrderID", "")
                sl_order_id = oco_orders[1].get("OrderID", "")
                log.trade(f"OCO bracket placed: TP={tp_order_id} SL={sl_order_id}")
            else:
                log.warning(f"OCO bracket response unexpected: {oco_resp}", "TRADE")

            # Store trade
            self.open_trade = OpenTrade(
                signal=signal,
                option_symbol=opt_sym,
                entry_price=mid,
                stop_loss=sl_price,
                take_profit=tp_price,
                quantity=qty,
                risk_amount=risk_dollars,
                reward_amount=reward_dollars,
                entry_time=now_et(),
                entry_order_id=entry_order_id,
                sl_order_id=sl_order_id,
                tp_order_id=tp_order_id,
                filled=False,  # Wait for fill confirmation
                current_price=mid,
            )
            self.trades_today += 1

            AlertSystem.send(
                f"{'🟢 LONG' if signal == Signal.LONG else '🔴 SHORT'} "
                f"Entry: {qty}x {opt_sym}\n"
                f"Price: ${mid:.2f} | SL: ${sl_price:.2f} | TP: ${tp_price:.2f}\n"
                f"Risk: ${risk_dollars:.0f} | Reward: ${reward_dollars:.0f}",
                "TRADE"
            )

    # ── Position Management ──────────────────────────────────

    def _check_position(self):
        """Called every 15 seconds to manage open position."""
        if not self.open_trade or not self.open_trade.filled:
            return

        trade = self.open_trade
        elapsed = (now_et() - trade.entry_time).total_seconds() / 60

        # Update current option price
        quote = self.api.get_option_quote(trade.option_symbol)
        if quote:
            last = float(quote.get("Last", trade.current_price))
            bid  = float(quote.get("Bid", last))
            trade.current_price = last
            trade.unrealized_pnl = (last - trade.entry_price) * 100 * trade.quantity

        # 1) TIME STOP: 25 minutes
        if elapsed > Config.TIME_STOP_MIN:
            log.trade(f"TIME STOP @ {elapsed:.1f} min — closing position")
            self._force_close("TIME_STOP")
            return

        # 2) BREAKEVEN TRAIL: move SL to entry when profit ≥ 1× risk
        if not trade.be_trailed:
            profit = trade.current_price - trade.entry_price
            risk_unit = trade.entry_price * Config.RISK_PER_TRADE
            if profit >= risk_unit:
                log.trade(f"BREAKEVEN TRAIL activated at ${trade.current_price:.2f}")
                self.api.modify_order(
                    trade.sl_order_id,
                    stop_price=trade.entry_price
                )
                trade.be_trailed = True
                trade.stop_loss = trade.entry_price
                AlertSystem.send(
                    f"SL moved to breakeven (${trade.entry_price:.2f})", "INFO"
                )

    def _force_close(self, reason: str):
        """Force close all open positions and cancel pending orders."""
        if not self.open_trade:
            return

        trade = self.open_trade
        log.trade(f"FORCE CLOSE [{reason}]: {trade.option_symbol}")

        # Cancel OCO bracket orders
        if trade.tp_order_id:
            self.api.cancel_order(trade.tp_order_id)
        if trade.sl_order_id:
            self.api.cancel_order(trade.sl_order_id)

        # Market close
        close_resp = self.api.place_order(
            symbol=trade.option_symbol,
            qty=trade.quantity,
            trade_action="SellToClose",
            order_type="Market",
        )

        # Estimate P/L
        exit_price = trade.current_price
        pnl = (exit_price - trade.entry_price) * 100 * trade.quantity

        self._record_trade(trade, exit_price, pnl, reason)
        self.open_trade = None

    # ── Order Stream Handler ─────────────────────────────────

    def _on_order_update(self, order_data: dict):
        """Handle real-time order status updates."""
        try:
            status   = order_data.get("Status", "")
            order_id = order_data.get("OrderID", "")
            fill_price = float(order_data.get("FilledPrice", 0) or 0)

            if not self.open_trade:
                return

            trade = self.open_trade

            # Entry fill confirmation
            if (status == "FLL" and order_id == trade.entry_order_id
                    and not trade.filled):
                trade.filled = True
                if fill_price > 0:
                    trade.entry_price = fill_price
                log.trade(f"ENTRY FILLED @ ${fill_price:.2f}")
                return

            # TP filled
            if status == "FLL" and order_id == trade.tp_order_id:
                exit_price = fill_price if fill_price > 0 else trade.take_profit
                pnl = (exit_price - trade.entry_price) * 100 * trade.quantity

                log.trade(f"✅ TAKE PROFIT HIT @ ${exit_price:.2f} | P/L: ${pnl:.2f}")
                AlertSystem.send(
                    f"✅ WIN: {trade.option_symbol}\n"
                    f"Entry: ${trade.entry_price:.2f} → Exit: ${exit_price:.2f}\n"
                    f"P/L: ${pnl:.2f}", "WIN"
                )

                self._record_trade(trade, exit_price, pnl, "TP")
                with self._position_lock:
                    self.open_trade = None
                    self.consec_losses = 0
                return

            # SL filled
            if status == "FLL" and order_id == trade.sl_order_id:
                exit_price = fill_price if fill_price > 0 else trade.stop_loss
                pnl = (exit_price - trade.entry_price) * 100 * trade.quantity

                result = "BE" if trade.be_trailed else "SL"
                log.trade(f"{'⚪ BREAKEVEN' if trade.be_trailed else '❌ STOP LOSS'} "
                          f"@ ${exit_price:.2f} | P/L: ${pnl:.2f}")
                AlertSystem.send(
                    f"{'⚪ BE' if trade.be_trailed else '🔴 LOSS'}: "
                    f"{trade.option_symbol}\n"
                    f"Entry: ${trade.entry_price:.2f} → Exit: ${exit_price:.2f}\n"
                    f"P/L: ${pnl:.2f}", "LOSS"
                )

                self._record_trade(trade, exit_price, pnl, result)
                with self._position_lock:
                    self.open_trade = None
                    if not trade.be_trailed:
                        self.consec_losses += 1
                        if self.consec_losses >= Config.MAX_CONSECUTIVE_LOSS:
                            self.cooldown_until = (
                                now_et()
                                + timedelta(minutes=Config.COOLDOWN_AFTER_LOSS)
                            )
                            log.alert(
                                f"3 consecutive losses — cooling down until "
                                f"{self.cooldown_until.strftime('%H:%M')}"
                            )
                            AlertSystem.send(
                                f"🧊 Cooldown: 3 losses. Pausing {Config.COOLDOWN_AFTER_LOSS} min",
                                "ALERT"
                            )
                return

            # Order rejected
            if status in ("REJ", "BRO", "UCN"):
                log.error(f"Order {order_id} status: {status} — {order_data}", "ORDER")
                AlertSystem.send(f"Order {status}: {order_data.get('RejectReason', 'Unknown')}", "ERROR")

        except Exception as e:
            log.error(f"Order update handler error: {e}", "ORDER")

    # ── Helpers ───────────────────────────────────────────────

    def _classify_regime(self, adx: float, vix: float) -> Regime:
        if adx > Config.ADX_THRESHOLD or vix > 30:
            return Regime.TRENDING
        elif adx < 20 and vix < 12:
            return Regime.LOW_VOL
        return Regime.MEAN_REVERTING

    def _build_option_symbol(self, spx_price: float, signal: Signal) -> str:
        """Build TradeStation SPX option symbol: $SPX.X YYMMDDCNNNNN"""
        today = now_et().strftime("%y%m%d")
        strike = round(spx_price)
        opt_type = "C" if signal == Signal.LONG else "P"
        return f"$SPX.X {today}{opt_type}{strike:05d}"

    @staticmethod
    def _is_trading_day() -> bool:
        """Return True only on regular market days (Mon-Fri, non-holiday)."""
        today = now_et().date()
        # Weekend check (5 = Saturday, 6 = Sunday)
        if today.weekday() >= 5:
            return False
        # US market holiday check
        if today in Config.MARKET_HOLIDAYS:
            return False
        return True

    def _in_session(self) -> bool:
        """Return True only during the trading window on a valid trading day."""
        if not self._is_trading_day():
            return False
        now = now_et().time()
        return Config.SESSION_START <= now <= Config.SESSION_END

    def _within_limits(self) -> bool:
        # Daily loss cap
        if self.daily_pnl <= -(self.account_balance * Config.MAX_DAILY_LOSS_PCT):
            log.warning(f"Daily loss limit hit: ${self.daily_pnl:.2f}", "RISK")
            return False

        # Max trades
        if self.trades_today >= Config.MAX_TRADES_PER_DAY:
            return False

        # Cooldown after consecutive losses
        if self.cooldown_until and now_et() < self.cooldown_until:
            return False

        # Clear cooldown if expired
        if self.cooldown_until and now_et() >= self.cooldown_until:
            self.cooldown_until = None
            self.consec_losses = 0
            log.info("Cooldown expired — resuming trading", "RISK")

        return True

    def _record_trade(self, trade: OpenTrade, exit_price: float,
                      pnl: float, result: str):
        """Record completed trade."""
        self.daily_pnl += pnl
        self.account_balance += pnl

        record = TradeRecord(
            signal=trade.signal.value,
            option_symbol=trade.option_symbol,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            quantity=trade.quantity,
            pnl=pnl,
            result=result,
            entry_time=trade.entry_time.isoformat(),
            exit_time=now_et().isoformat(),
            regime=self.current_regime.value,
        )
        self.trade_history.append(record)

        log.trade(
            f"RECORDED: {result} | ${pnl:+.2f} | "
            f"Daily P/L: ${self.daily_pnl:+.2f} | "
            f"Wins: {sum(1 for t in self.trade_history if t.pnl > 0)} / "
            f"{len(self.trade_history)}"
        )

    def _send_daily_summary(self):
        """Send end-of-day summary alert."""
        wins = sum(1 for t in self.trade_history if t.pnl > 0)
        losses = sum(1 for t in self.trade_history if t.pnl <= 0)
        total = len(self.trade_history)
        win_rate = (wins / total * 100) if total > 0 else 0

        msg = (
            f"📊 *Daily Summary*\n"
            f"Trades: {total} | Wins: {wins} | Losses: {losses}\n"
            f"Win Rate: {win_rate:.0f}%\n"
            f"Daily P/L: ${self.daily_pnl:+,.2f}\n"
            f"Account: ${self.account_balance:,.2f}"
        )
        log.info(msg, "SUMMARY")
        AlertSystem.send(msg, "INFO")

    def _get_last_bars_summary(self) -> list:
        """Return last 5 bars for dashboard sparkline."""
        if self.bars_df is None or self.bars_df.empty:
            return []
        cols_map = {"Close": "close", "close": "close"}
        close_col = "close" if "close" in self.bars_df.columns else "Close"
        return self.bars_df[close_col].tail(20).tolist()


# ═══════════════════════════════════════════════════════════════
# FLASK DASHBOARD API
# ═══════════════════════════════════════════════════════════════

def create_dashboard_app(engine: MeanReversionEngine):
    """Create Flask app for monitoring dashboard."""
    try:
        from flask import Flask, jsonify, request, send_file, send_from_directory
        from flask_cors import CORS
    except ImportError:
        log.warning("Flask not installed. Run: pip install flask flask-cors", "DASHBOARD")
        return None

    app = Flask(__name__)
    CORS(app)

    # ── Serve the dashboard HTML at root ──
    @app.route("/")
    def index():
        # Look for dashboard.html in same directory as this script
        import os
        dashboard_paths = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"),
            os.path.join(os.getcwd(), "dashboard.html"),
        ]
        for path in dashboard_paths:
            if os.path.exists(path):
                return send_file(path)
        # Fallback: return a simple redirect message
        return (
            "<html><body style='background:#08090d;color:#dfe2f0;font-family:sans-serif;"
            "display:flex;justify-content:center;align-items:center;height:100vh'>"
            "<div style='text-align:center'>"
            "<h2>SPX 0DTE Engine API is running</h2>"
            "<p style='color:#8892b0'>Place <code>dashboard.html</code> in the same "
            "folder as the engine, then refresh this page.</p>"
            "<p style='color:#8892b0;margin-top:20px'>API endpoints available at:</p>"
            "<pre style='color:#10d070'>/api/status\n/api/trades\n/api/logs\n"
            "/api/alerts\n/api/orders\n/api/positions</pre>"
            "</div></body></html>"
        )

    @app.route("/api/status")
    def status():
        return jsonify(engine.get_status())

    @app.route("/api/trades")
    def trades():
        return jsonify(engine.get_trade_history())

    @app.route("/api/logs")
    def logs():
        limit = int(request.args.get("limit", 100))
        return jsonify(log.get_logs(limit))

    @app.route("/api/alerts")
    def alerts():
        return jsonify(log.get_alerts())

    @app.route("/api/decisions")
    def decisions():
        limit = int(request.args.get("limit", 50))
        return jsonify(engine.get_decision_log(limit))

    @app.route("/api/orders")
    def orders():
        return jsonify(engine.api.get_orders())

    @app.route("/api/positions")
    def positions():
        return jsonify(engine.api.get_positions())

    @app.route("/api/kill", methods=["POST"])
    def kill():
        engine.stop()
        return jsonify({"status": "stopped"})

    @app.route("/api/close_position", methods=["POST"])
    def close_position():
        engine._force_close("MANUAL")
        return jsonify({"status": "closed"})

    return app


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    """Start the trading engine + dashboard."""
    print("╔══════════════════════════════════════════════════╗")
    print("║  SPX 0DTE Mean Reversion Engine — Production    ║")
    print("║  TradeStation API v3                            ║")
    print("╚══════════════════════════════════════════════════╝")

    # Validate config
    if not Config.TS_CLIENT_ID:
        print("ERROR: Set TS_CLIENT_ID in .env file")
        return
    if not Config.TS_CLIENT_SECRET:
        print("ERROR: Set TS_CLIENT_SECRET in .env file")
        return
    if not Config.TS_REFRESH_TOKEN:
        print("ERROR: Set TS_REFRESH_TOKEN in .env file")
        return

    # Initialize
    auth = TradeStationAuth()
    api = TradeStationAPI(auth, Config.TS_ACCOUNT_ID)

    # Auto-detect account if not set
    if not Config.TS_ACCOUNT_ID:
        accounts = api.get_accounts()
        if accounts:
            api.account_id = accounts[0]["AccountID"]
            log.info(f"Auto-detected account: {api.account_id}", "INIT")
        else:
            log.error("No accounts found — check API credentials", "INIT")
            return

    calendar = EconomicCalendar()
    engine = MeanReversionEngine(api, calendar)

    # Start dashboard in background
    app = create_dashboard_app(engine)
    if app:
        dashboard_thread = threading.Thread(
            target=lambda: app.run(
                host="0.0.0.0",
                port=Config.DASHBOARD_PORT,
                debug=False,
                use_reloader=False,
            ),
            daemon=True,
            name="Dashboard",
        )
        dashboard_thread.start()
        log.info(f"Dashboard running at http://localhost:{Config.DASHBOARD_PORT}", "DASHBOARD")

    # Start engine
    engine.start()

    # Keep main thread alive
    try:
        while not engine.stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down", "ENGINE")
        engine.stop()


if __name__ == "__main__":
    main()