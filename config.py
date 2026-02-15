"""
╔══════════════════════════════════════════════════════════════════╗
║  SPX 0DTE CONFIG — Edit this file only                          ║
║                                                                  ║
║  The engine (spx_0dte_engine.py) imports Config from here.      ║
║  When upgrading the engine, just replace the engine file —      ║
║  your settings here are preserved.                               ║
║                                                                  ║
║  Priority: values set here > .env file > defaults               ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
from datetime import time as dtime
from dotenv import load_dotenv

load_dotenv()


class Config:
    """
    Central configuration for the SPX 0DTE Mean Reversion Engine.

    HOW TO CUSTOMIZE:
    - Edit values directly below, OR
    - Set them as environment variables in your .env file
    - Values set here take precedence over .env defaults
    """

    # ═══════════════════════════════════════════════════════════
    # TRADESTATION API CREDENTIALS
    # Get these from: https://developer.tradestation.com
    # ═══════════════════════════════════════════════════════════

    TS_CLIENT_ID     = os.getenv("TS_CLIENT_ID", "qdWl7XZlhJAgfGX5lXjsLKIYwJHIblSB")
    TS_CLIENT_SECRET = os.getenv("TS_CLIENT_SECRET", "t9ZmqD5bBQM4Gqk7lnoEDqxTqD0NM9_GvLyiIBa4-WhU0WnhP46YIhkVN822FlTT")
    TS_REFRESH_TOKEN = os.getenv("TS_REFRESH_TOKEN", "dksi0aBxm2QNWcsxB-XGBO5cRzFMtYiX2-u1KmjDgkxbi")
    

    # "SIM" for paper trading, "LIVE" for real money
    # ⚠️  ALWAYS START WITH SIM — switch to LIVE only after 30+ days paper trading
    TS_ENVIRONMENT   = os.getenv("TS_ENVIRONMENT", "SIM")

    # Leave blank to auto-detect the first account on your API key
    TS_ACCOUNT_ID    = os.getenv("TS_ACCOUNT_ID", "SIM3170696M")

    # API Base URLs (do not change unless TradeStation updates these)
    BASE_URL_LIVE = "https://api.tradestation.com"
    BASE_URL_SIM  = "https://sim-api.tradestation.com"

    @classmethod
    def base_url(cls):
        return cls.BASE_URL_LIVE if cls.TS_ENVIRONMENT == "LIVE" else cls.BASE_URL_SIM

    # ═══════════════════════════════════════════════════════════
    # ALERT WEBHOOKS (Optional — leave blank to disable)
    # ═══════════════════════════════════════════════════════════

    # Telegram: Create bot via @BotFather, get chat_id via @userinfobot
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    # Discord: Server Settings → Integrations → Webhooks → Copy URL
    DISCORD_WEBHOOK    = os.getenv("DISCORD_WEBHOOK", "")

    # ═══════════════════════════════════════════════════════════
    # STRATEGY PARAMETERS
    # These control signal detection — tune carefully
    # ═══════════════════════════════════════════════════════════

    BB_PERIOD       = 20        # Bollinger Band lookback (bars)
    BB_STD          = 2.0       # Bollinger Band standard deviations
    RSI_PERIOD      = 9         # RSI lookback (bars)
    RSI_OVERBOUGHT  = 75        # RSI overbought threshold (short signal)
    RSI_OVERSOLD    = 25        # RSI oversold threshold (long signal)
    ADX_PERIOD      = 14        # ADX lookback (bars)
    ADX_THRESHOLD   = 25        # ADX below this = mean reverting regime
    VWAP_OFFSET     = 0.003     # Min VWAP deviation (0.3%) to qualify
    VOL_MULTIPLIER  = 1.2       # Volume must exceed this × 20-bar avg
    RR_RATIO        = 2.0       # Risk:Reward ratio (1:2 = TP at 2× risk)
    TIME_STOP_MIN   = 25        # Close if trade open longer than N minutes
    RISK_PER_TRADE  = 0.33      # Risk this fraction of option premium

    # ═══════════════════════════════════════════════════════════
    # RISK MANAGEMENT
    # Hard limits that protect your account
    # ═══════════════════════════════════════════════════════════

    MAX_DAILY_LOSS_PCT    = 0.03    # Stop trading if day P/L ≤ −3% of account
    MAX_CONSECUTIVE_LOSS  = 3       # Pause after N consecutive losses
    MAX_TRADES_PER_DAY    = 8       # Max trades allowed per session
    MAX_POSITION_PCT      = 0.02    # Max 2% of account risked per trade
    MAX_SPREAD            = 1.50    # Skip option if bid-ask spread > $1.50
    COOLDOWN_AFTER_LOSS   = 30      # Minutes to pause after hitting consec loss limit

    # ═══════════════════════════════════════════════════════════
    # SESSION TIMES (Eastern Time)
    # ═══════════════════════════════════════════════════════════

    SESSION_START   = dtime(9, 45)      # Skip first 15 min of market open
    SESSION_END     = dtime(15, 30)     # Stop monitoring at 3:30 PM
    NO_ENTRY_AFTER  = dtime(14, 0)      # No new entries after 2:00 PM (theta decay)
    EOD_CLOSE_TIME  = dtime(15, 25)     # Force close all positions at 3:25 PM

    # ═══════════════════════════════════════════════════════════
    # MONITORING & INFRASTRUCTURE
    # ═══════════════════════════════════════════════════════════

    POSITION_CHECK_INTERVAL = 15    # Seconds between position checks (SL/TP/time stop)
    VIX_CACHE_SECONDS       = 60    # Cache VIX quote to avoid rate limits
    STREAM_RECONNECT_MAX    = 5     # Max reconnect attempts before alerting
    STREAM_RECONNECT_BASE   = 2     # Base seconds for exponential backoff

    # ═══════════════════════════════════════════════════════════
    # DASHBOARD
    # ═══════════════════════════════════════════════════════════

    DASHBOARD_PORT = 5000               # Flask dashboard port
    LOG_FILE       = "trading_engine.log"
    MAX_LOG_LINES  = 500

    # ═══════════════════════════════════════════════════════════
    # US MARKET HOLIDAYS (update annually)
    # NYSE/CBOE observed holidays — engine will not trade on these
    # ═══════════════════════════════════════════════════════════

    from datetime import date as _date
    MARKET_HOLIDAYS = [
        # 2025
        _date(2025, 1, 1),    # New Year's Day
        _date(2025, 1, 20),   # Martin Luther King Jr. Day
        _date(2025, 2, 17),   # Presidents' Day
        _date(2025, 4, 18),   # Good Friday
        _date(2025, 5, 26),   # Memorial Day
        _date(2025, 6, 19),   # Juneteenth
        _date(2025, 7, 4),    # Independence Day
        _date(2025, 9, 1),    # Labor Day
        _date(2025, 11, 27),  # Thanksgiving Day
        _date(2025, 12, 25),  # Christmas Day
        # 2026
        _date(2026, 1, 1),    # New Year's Day
        _date(2026, 1, 19),   # Martin Luther King Jr. Day
        _date(2026, 2, 16),   # Presidents' Day
        _date(2026, 4, 3),    # Good Friday
        _date(2026, 5, 25),   # Memorial Day
        _date(2026, 6, 19),   # Juneteenth
        _date(2026, 7, 3),    # Independence Day (observed)
        _date(2026, 9, 7),    # Labor Day
        _date(2026, 11, 26),  # Thanksgiving Day
        _date(2026, 12, 25),  # Christmas Day
    ]
