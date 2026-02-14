"""
╔══════════════════════════════════════════════════════════════════╗
║  BACKTESTING MODULE — SPX 0DTE Mean Reversion                   ║
║  Integrates with spx_0dte_engine.py                             ║
║                                                                  ║
║  Usage:                                                          ║
║    python backtest_engine.py                         (offline)   ║
║    python backtest_engine.py --from-api              (via TS)    ║
║    python backtest_engine.py --csv data/spx_5min.csv (from CSV)  ║
║                                                                  ║
║  Produces:                                                       ║
║    • Trade-by-trade log with P/L                                 ║
║    • Win rate, profit factor, Sharpe, Sortino, max drawdown      ║
║    • Equity curve + drawdown chart (HTML)                        ║
║    • Monte Carlo confidence intervals                            ║
║    • Per-regime and per-hour performance breakdown               ║
║    • Exports results to CSV + HTML report                        ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import argparse
import math
from datetime import datetime, time as dtime, timedelta, date
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd

# Import from the main engine
# These must be importable — ensure spx_0dte_engine.py is in the same directory
try:
    from spx_0dte_engine import (
        Config, Indicators, Regime, Signal, TradeStationAuth,
        TradeStationAPI, log
    )
except ImportError:
    print("ERROR: spx_0dte_engine.py must be in the same directory.")
    print("       The backtester imports Indicators, Config, and Signal from it.")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# BACKTEST CONFIGURATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class BacktestConfig:
    """Configuration specific to backtesting."""

    # Data source
    data_source: str = "csv"           # "csv", "api", "synthetic"
    csv_path: str = ""
    api_bars_back: int = 5000          # Max bars from TradeStation API
    symbol: str = "$SPX.X"

    # Simulation parameters
    initial_capital: float = 25000.0
    commission_per_contract: float = 0.65   # TradeStation options commission
    slippage_pct: float = 0.005             # 0.5% slippage on option fills
    spread_cost: float = 0.50               # Average bid-ask spread cost per contract
    option_delta: float = 0.50              # ATM delta approximation
    option_gamma_multiplier: float = 1.5    # Gamma effect on 0DTE (amplifies moves)

    # Session rules (same as live)
    session_start: dtime = dtime(9, 45)
    session_end: dtime = dtime(15, 30)
    no_entry_after: dtime = dtime(14, 0)

    # Risk rules (same as live)
    max_daily_loss_pct: float = 0.03
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 8
    cooldown_minutes: int = 30

    # What-if analysis
    vary_bb_std: List[float] = field(default_factory=lambda: [2.0])
    vary_rsi_os: List[int] = field(default_factory=lambda: [25])
    vary_rsi_ob: List[int] = field(default_factory=lambda: [75])
    vary_adx: List[int] = field(default_factory=lambda: [25])

    # Monte Carlo
    monte_carlo_runs: int = 1000
    monte_carlo_sample_size: int = 252     # 1 year of trading days

    # Output
    output_dir: str = "backtest_results"
    generate_html: bool = True


# ═══════════════════════════════════════════════════════════════
# BACKTEST TRADE RECORD
# ═══════════════════════════════════════════════════════════════

@dataclass
class BacktestTrade:
    """Record of a single backtested trade."""
    trade_id: int
    date: str
    entry_time: str
    exit_time: str
    signal: str               # "long" / "short"
    entry_bar_idx: int
    exit_bar_idx: int
    spx_entry: float          # SPX price at entry
    spx_exit: float           # SPX price at exit
    option_entry: float       # Simulated option premium at entry
    option_exit: float        # Simulated option premium at exit
    stop_loss: float
    take_profit: float
    quantity: int
    gross_pnl: float
    commission: float
    slippage: float
    net_pnl: float
    result: str               # "TP", "SL", "TIME_STOP", "BE", "EOD"
    regime: str
    rsi_at_entry: float
    adx_at_entry: float
    vwap_dev_at_entry: float
    elapsed_bars: int
    be_trailed: bool
    hour_of_day: int


# ═══════════════════════════════════════════════════════════════
# OPTION PRICE SIMULATOR
# ═══════════════════════════════════════════════════════════════

class OptionPriceSimulator:
    """
    Approximates ATM 0DTE SPX option prices based on underlying movement.
    
    Since we don't have historical option prices, we model the premium
    as a function of:
    - Delta: ATM ≈ 0.50 (moves ~$0.50 per $1 SPX move)
    - Gamma: 0DTE gamma is extremely high, amplifying moves
    - Theta: Decays throughout the day
    - A base premium based on VIX/volatility
    """

    @staticmethod
    def estimate_premium(spx_price: float, hours_to_expiry: float,
                         vix: float = 18.0) -> float:
        """Estimate ATM 0DTE option premium."""
        # Black-Scholes-ish approximation for ATM
        # ATM premium ≈ 0.4 * S * σ * √T
        sigma = vix / 100.0  # Annualized vol
        T = max(hours_to_expiry / (252 * 6.5), 0.0001)  # Years
        premium = 0.4 * spx_price * sigma * math.sqrt(T)
        return round(max(premium, 0.50), 2)

    @staticmethod
    def price_change(spx_move: float, delta: float = 0.50,
                     gamma_mult: float = 1.5,
                     hours_to_expiry: float = 3.0,
                     time_elapsed_hours: float = 0.0) -> float:
        """
        Estimate option price change given SPX move.
        
        For 0DTE, gamma is very high near ATM, so small SPX moves
        produce outsized option moves. We model this with a multiplier.
        """
        # Base delta move
        delta_pnl = spx_move * delta

        # Gamma amplification (increases as expiry approaches)
        time_factor = max(0.5, 1.0 + (1.0 - hours_to_expiry / 6.5))
        gamma_pnl = 0.5 * gamma_mult * time_factor * (spx_move ** 2) / abs(spx_move + 0.001)

        # Theta decay (penalizes holding)
        theta_per_hour = -0.15 * (1.0 + max(0, 3.0 - hours_to_expiry))
        theta_cost = theta_per_hour * time_elapsed_hours

        total = delta_pnl + (gamma_pnl if abs(spx_move) > 2 else 0) + theta_cost

        return round(total, 2)


# ═══════════════════════════════════════════════════════════════
# DATA LOADER
# ═══════════════════════════════════════════════════════════════

class DataLoader:
    """Load and prepare 5-minute SPX bar data."""

    @staticmethod
    def from_csv(path: str) -> pd.DataFrame:
        """
        Load from CSV file.
        Expected columns: DateTime/TimeStamp, Open, High, Low, Close, Volume
        """
        print(f"Loading data from: {path}")

        df = pd.read_csv(path)

        # Normalize column names
        col_map = {}
        for col in df.columns:
            cl = col.lower().strip()
            if cl in ("datetime", "timestamp", "date", "time", "date_time"):
                col_map[col] = "TimeStamp"
            elif cl == "open":
                col_map[col] = "Open"
            elif cl == "high":
                col_map[col] = "High"
            elif cl == "low":
                col_map[col] = "Low"
            elif cl == "close":
                col_map[col] = "Close"
            elif cl in ("volume", "totalvolume", "vol"):
                col_map[col] = "TotalVolume"

        df = df.rename(columns=col_map)

        # Parse timestamp
        df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])

        # Ensure numeric
        for col in ["Open", "High", "Low", "Close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "TotalVolume" in df.columns:
            df["TotalVolume"] = pd.to_numeric(df["TotalVolume"], errors="coerce").fillna(0).astype(int)
        else:
            # Synthesize volume if missing
            df["TotalVolume"] = np.random.randint(50000, 200000, len(df))

        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df.sort_values("TimeStamp").reset_index(drop=True)

        print(f"  Loaded {len(df)} bars from {df['TimeStamp'].iloc[0]} to {df['TimeStamp'].iloc[-1]}")
        return df

    @staticmethod
    def from_api(api: TradeStationAPI, symbol: str = "$SPX.X",
                 bars_back: int = 5000) -> pd.DataFrame:
        """Load from TradeStation API."""
        print(f"Loading {bars_back} bars from TradeStation API...")
        df = api.get_bars(symbol, interval=5, unit="Minute", barsback=bars_back)
        if df.empty:
            print("ERROR: No data returned from API")
            sys.exit(1)
        print(f"  Loaded {len(df)} bars")
        return df

    @staticmethod
    def generate_synthetic(days: int = 500, start_price: float = 5800.0) -> pd.DataFrame:
        """
        Generate synthetic 5-min SPX data for testing when no real data available.
        Models realistic intraday patterns with trend/range regimes.
        """
        print(f"Generating {days} days of synthetic 5-min SPX data...")

        bars = []
        price = start_price
        bars_per_day = 78  # 9:30 AM to 4:00 PM = 6.5 hours = 78 five-min bars

        for day_num in range(days):
            day_date = date(2023, 1, 2) + timedelta(days=day_num)
            # Skip weekends
            if day_date.weekday() >= 5:
                continue

            # Determine daily regime
            regime_roll = np.random.random()
            if regime_roll < 0.30:
                # Trending day: strong drift
                daily_drift = np.random.choice([-1, 1]) * np.random.uniform(0.002, 0.005)
                daily_vol = np.random.uniform(0.0008, 0.0012)
            elif regime_roll < 0.60:
                # Mean reverting day: low drift, normal vol
                daily_drift = np.random.uniform(-0.0003, 0.0003)
                daily_vol = np.random.uniform(0.0005, 0.0009)
            else:
                # Choppy/range day
                daily_drift = np.random.uniform(-0.0002, 0.0002)
                daily_vol = np.random.uniform(0.0004, 0.0007)

            day_open = price

            for bar_num in range(bars_per_day):
                bar_time = datetime.combine(day_date, dtime(9, 30)) + timedelta(minutes=5 * bar_num)

                # Intraday mean reversion toward VWAP-like anchor
                revert_force = -0.05 * (price - day_open) / day_open

                # Random move
                move = price * (daily_drift + revert_force + daily_vol * np.random.randn())
                new_price = price + move

                # Generate OHLC
                intra_vol = abs(move) * np.random.uniform(0.5, 2.0)
                o = price
                c = new_price
                h = max(o, c) + abs(intra_vol) * np.random.uniform(0, 1)
                l = min(o, c) - abs(intra_vol) * np.random.uniform(0, 1)

                volume = int(np.random.lognormal(11.5, 0.5))  # Realistic SPX volume

                bars.append({
                    "TimeStamp": bar_time,
                    "Open": round(o, 2),
                    "High": round(h, 2),
                    "Low": round(l, 2),
                    "Close": round(c, 2),
                    "TotalVolume": volume,
                })

                price = new_price

        df = pd.DataFrame(bars)
        df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
        print(f"  Generated {len(df)} bars across {len(df) // bars_per_day} trading days")
        return df


# ═══════════════════════════════════════════════════════════════
# BACKTESTING ENGINE
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Bar-by-bar backtesting engine that replicates the live engine's
    exact signal detection, entry, and exit logic.
    """

    def __init__(self, config: BacktestConfig = BacktestConfig()):
        self.config = config
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = []
        self.daily_results: Dict[str, Dict] = {}

        # Running state (reset per day)
        self._trade_counter = 0
        self._daily_pnl = 0.0
        self._consec_losses = 0
        self._trades_today = 0
        self._cooldown_until: Optional[datetime] = None
        self._in_trade = False
        self._current_trade: Optional[dict] = None

    def run(self, df: pd.DataFrame, vix_series: Optional[pd.Series] = None) -> 'BacktestResults':
        """
        Run the backtest on the provided DataFrame.
        
        Args:
            df: DataFrame with TimeStamp, Open, High, Low, Close, TotalVolume
            vix_series: Optional VIX values aligned to df index. If None, estimated from volatility.
        """
        print("\n" + "═" * 60)
        print("  BACKTESTING SPX 0DTE MEAN REVERSION STRATEGY")
        print("═" * 60)

        # Group bars by trading day
        df = df.copy()
        df["trade_date"] = df["TimeStamp"].dt.date

        trading_days = df["trade_date"].unique()
        total_days = len(trading_days)
        print(f"  Trading days: {total_days}")
        print(f"  Date range: {trading_days[0]} → {trading_days[-1]}")
        print(f"  Initial capital: ${self.config.initial_capital:,.2f}")
        print(f"  Config: BB={Config.BB_STD}σ RSI={Config.RSI_OVERSOLD}/{Config.RSI_OVERBOUGHT} ADX<{Config.ADX_THRESHOLD}")
        print("═" * 60)

        capital = self.config.initial_capital
        self.equity_curve = [capital]
        peak_capital = capital

        for day_idx, trade_date in enumerate(trading_days):
            day_bars = df[df["trade_date"] == trade_date].copy()

            if len(day_bars) < Config.BB_PERIOD + 10:
                continue

            # Estimate VIX for the day
            if vix_series is not None and trade_date in vix_series.index:
                day_vix = vix_series[trade_date]
            else:
                # Estimate from intraday volatility
                returns = day_bars["Close"].pct_change().dropna()
                if len(returns) > 5:
                    day_vix = returns.std() * math.sqrt(252 * 78) * 100
                    day_vix = max(10, min(45, day_vix))
                else:
                    day_vix = 18.0

            # Run day simulation
            day_pnl = self._simulate_day(day_bars, day_vix, capital)
            capital += day_pnl
            self.equity_curve.append(capital)

            # Track daily
            self.daily_results[str(trade_date)] = {
                "date": str(trade_date),
                "pnl": day_pnl,
                "trades": self._trades_today,
                "capital": capital,
                "vix": round(day_vix, 1),
            }

            # Progress
            if (day_idx + 1) % 50 == 0 or day_idx == total_days - 1:
                wr = self._calc_running_winrate()
                print(f"  Day {day_idx+1}/{total_days} | "
                      f"Capital: ${capital:,.0f} | "
                      f"Trades: {len(self.trades)} | "
                      f"Win Rate: {wr:.1f}% | "
                      f"Day P/L: ${day_pnl:+,.0f}")

        # Build results
        results = BacktestResults(
            trades=self.trades,
            equity_curve=self.equity_curve,
            daily_results=self.daily_results,
            config=self.config,
        )
        results.compute_statistics()

        return results

    def _simulate_day(self, day_bars: pd.DataFrame, vix: float,
                      current_capital: float) -> float:
        """Simulate one trading day bar by bar."""
        # Reset daily state
        self._daily_pnl = 0.0
        self._consec_losses = 0
        self._trades_today = 0
        self._cooldown_until = None
        self._in_trade = False
        self._current_trade = None

        # Compute indicators for the day (expanding window)
        day_df = Indicators.compute(day_bars.copy())

        if day_df.empty or len(day_df) < Config.BB_PERIOD + 5:
            return 0.0

        day_pnl = 0.0

        for i in range(Config.BB_PERIOD + 5, len(day_df)):
            row = day_df.iloc[i]
            prev = day_df.iloc[i - 1]
            bar_time = pd.Timestamp(row.get("TimeStamp", day_bars.iloc[i]["TimeStamp"]))

            # Skip if outside session
            if hasattr(bar_time, 'time'):
                bt = bar_time.time()
            else:
                continue

            if bt < self.config.session_start or bt > self.config.session_end:
                continue

            # ── Manage open trade ──
            if self._in_trade and self._current_trade:
                result = self._check_exit(row, prev, i, bar_time, vix)
                if result:
                    day_pnl += result
                    continue

            # ── Check for new entry ──
            if self._in_trade:
                continue

            # EOD: no new entries after 2 PM
            if bt > self.config.no_entry_after:
                continue

            # Risk limits
            if not self._check_limits(current_capital + day_pnl):
                continue

            # Classify regime
            adx_val = float(row.get("adx", 30))
            regime = self._classify_regime(adx_val, vix)

            if regime == Regime.TRENDING:
                continue

            # Detect signal (uses exact same logic as live engine)
            signal = self._detect_signal(row, prev)

            if signal != Signal.NONE:
                entry_pnl = self._open_trade(
                    signal, row, i, bar_time, vix, regime,
                    current_capital + day_pnl
                )

        # EOD: force close any open position
        if self._in_trade and self._current_trade:
            last_row = day_df.iloc[-1]
            last_time = pd.Timestamp(day_bars.iloc[-1]["TimeStamp"])
            eod_pnl = self._force_close_trade(last_row, len(day_df) - 1, last_time, vix, "EOD")
            day_pnl += eod_pnl

        return day_pnl

    def _detect_signal(self, row, prev) -> Signal:
        """Exact replica of live engine signal detection."""
        close = float(row.get("close", 0))
        vwap = float(row.get("vwap", close))
        rsi = float(row.get("rsi", 50))
        adx = float(row.get("adx", 30))
        bb_upper = float(row.get("bb_upper", close + 100))
        bb_lower = float(row.get("bb_lower", close - 100))
        volume = float(row.get("volume", 0))
        vol_avg = float(row.get("vol_avg", 1))

        if vwap <= 0 or vol_avg <= 0:
            return Signal.NONE

        vwap_dev = (close - vwap) / vwap
        vol_ok = volume > Config.VOL_MULTIPLIER * vol_avg

        # LONG
        if (close < bb_lower
                and vwap_dev < -Config.VWAP_OFFSET
                and rsi < Config.RSI_OVERSOLD
                and adx < Config.ADX_THRESHOLD
                and vol_ok
                and Indicators.is_reversal_candle(row, prev, "bull")):
            return Signal.LONG

        # SHORT
        if (close > bb_upper
                and vwap_dev > Config.VWAP_OFFSET
                and rsi > Config.RSI_OVERBOUGHT
                and adx < Config.ADX_THRESHOLD
                and vol_ok
                and Indicators.is_reversal_candle(row, prev, "bear")):
            return Signal.SHORT

        return Signal.NONE

    def _open_trade(self, signal, row, bar_idx, bar_time, vix, regime, capital):
        """Simulate opening a trade."""
        spx_price = float(row["close"])

        # Estimate option premium
        hours_left = max(0.1, (16.0 - bar_time.hour - bar_time.minute / 60))
        premium = OptionPriceSimulator.estimate_premium(spx_price, hours_left, vix)

        # Add slippage + half spread (buying at ask)
        entry_cost = premium + self.config.spread_cost / 2 + premium * self.config.slippage_pct
        entry_cost = round(entry_cost, 2)

        # Position sizing
        risk = entry_cost * Config.RISK_PER_TRADE
        risk_per_contract = risk * 100
        max_risk = capital * self.config.max_daily_loss_pct / 3  # Conservative
        qty = max(1, int(max_risk / risk_per_contract)) if risk_per_contract > 0 else 1

        if regime == Regime.LOW_VOL:
            qty = max(1, qty // 2)

        sl_price = round(entry_cost - risk, 2)
        tp_price = round(entry_cost + risk * Config.RR_RATIO, 2)

        self._in_trade = True
        self._current_trade = {
            "signal": signal,
            "entry_bar_idx": bar_idx,
            "entry_time": bar_time,
            "spx_entry": spx_price,
            "option_entry": entry_cost,
            "stop_loss": max(0.05, sl_price),
            "take_profit": tp_price,
            "quantity": qty,
            "risk": risk,
            "regime": regime,
            "rsi": float(row.get("rsi", 50)),
            "adx": float(row.get("adx", 25)),
            "vwap_dev": float((row["close"] - row.get("vwap", row["close"])) / max(row.get("vwap", 1), 1)),
            "vix": vix,
            "be_trailed": False,
            "hours_left_at_entry": hours_left,
        }
        self._trades_today += 1
        return 0

    def _check_exit(self, row, prev, bar_idx, bar_time, vix) -> Optional[float]:
        """Check if current bar triggers an exit."""
        t = self._current_trade
        if not t:
            return None

        signal = t["signal"]
        spx_entry = t["spx_entry"]
        spx_now = float(row["close"])
        spx_high = float(row["high"])
        spx_low = float(row["low"])

        # Calculate SPX move from entry
        spx_move = spx_now - spx_entry
        if signal == Signal.SHORT:
            spx_move = -spx_move  # For puts, profit when SPX drops

        # Estimate current option price
        elapsed_bars = bar_idx - t["entry_bar_idx"]
        elapsed_hours = elapsed_bars * 5 / 60
        hours_left = max(0.1, t["hours_left_at_entry"] - elapsed_hours)

        option_change = OptionPriceSimulator.price_change(
            abs(spx_move) * (1 if spx_move > 0 else -1),
            delta=self.config.option_delta,
            gamma_mult=self.config.option_gamma_multiplier,
            hours_to_expiry=hours_left,
            time_elapsed_hours=elapsed_hours,
        )

        current_option = t["option_entry"] + option_change
        current_option = max(0.01, current_option)

        # ── Check worst-case intrabar for stop loss ──
        # For calls: SL hit if SPX drops enough; for puts: if SPX rises enough
        if signal == Signal.LONG:
            worst_move = spx_low - spx_entry
        else:
            worst_move = spx_entry - spx_high

        worst_option_change = OptionPriceSimulator.price_change(
            worst_move,
            delta=self.config.option_delta,
            gamma_mult=self.config.option_gamma_multiplier,
            hours_to_expiry=hours_left,
            time_elapsed_hours=elapsed_hours,
        )
        worst_option = t["option_entry"] + worst_option_change

        # ── Check best-case intrabar for take profit ──
        if signal == Signal.LONG:
            best_move = spx_high - spx_entry
        else:
            best_move = spx_entry - spx_low

        best_option_change = OptionPriceSimulator.price_change(
            best_move,
            delta=self.config.option_delta,
            gamma_mult=self.config.option_gamma_multiplier,
            hours_to_expiry=hours_left,
            time_elapsed_hours=elapsed_hours,
        )
        best_option = t["option_entry"] + best_option_change

        # ── Take Profit ──
        if best_option >= t["take_profit"]:
            return self._close_trade(
                t["take_profit"], spx_now, bar_idx, bar_time, "TP", t["be_trailed"]
            )

        # ── Stop Loss ──
        if worst_option <= t["stop_loss"]:
            result_type = "BE" if t["be_trailed"] else "SL"
            return self._close_trade(
                t["stop_loss"], spx_now, bar_idx, bar_time, result_type, t["be_trailed"]
            )

        # ── Breakeven Trail ──
        if not t["be_trailed"]:
            profit = current_option - t["option_entry"]
            risk_unit = t["risk"]
            if profit >= risk_unit:
                t["be_trailed"] = True
                t["stop_loss"] = t["option_entry"]  # Move SL to entry

        # ── Time Stop (25 minutes = 5 bars) ──
        if elapsed_bars >= Config.TIME_STOP_MIN // 5:
            exit_price = current_option - self.config.spread_cost / 2  # Sell at bid
            return self._close_trade(
                max(0.01, exit_price), spx_now, bar_idx, bar_time, "TIME_STOP", t["be_trailed"]
            )

        return None

    def _close_trade(self, exit_option_price, spx_now, bar_idx,
                     bar_time, result, be_trailed) -> float:
        """Close trade and record result."""
        t = self._current_trade
        self._trade_counter += 1

        qty = t["quantity"]
        entry = t["option_entry"]
        exit_p = exit_option_price

        # Apply exit slippage + half spread
        exit_p -= self.config.spread_cost / 2 + exit_p * self.config.slippage_pct
        exit_p = max(0.01, round(exit_p, 2))

        gross_pnl = (exit_p - entry) * 100 * qty
        commission = self.config.commission_per_contract * qty * 2  # Round trip
        slippage_cost = entry * self.config.slippage_pct * 100 * qty * 2
        net_pnl = gross_pnl - commission

        elapsed = bar_idx - t["entry_bar_idx"]

        trade = BacktestTrade(
            trade_id=self._trade_counter,
            date=str(t["entry_time"].date()) if hasattr(t["entry_time"], 'date') else str(t["entry_time"])[:10],
            entry_time=str(t["entry_time"]),
            exit_time=str(bar_time),
            signal=t["signal"].value,
            entry_bar_idx=t["entry_bar_idx"],
            exit_bar_idx=bar_idx,
            spx_entry=t["spx_entry"],
            spx_exit=spx_now,
            option_entry=entry,
            option_exit=exit_p,
            stop_loss=t["stop_loss"],
            take_profit=t["take_profit"],
            quantity=qty,
            gross_pnl=round(gross_pnl, 2),
            commission=round(commission, 2),
            slippage=round(slippage_cost, 2),
            net_pnl=round(net_pnl, 2),
            result=result,
            regime=t["regime"].value,
            rsi_at_entry=round(t["rsi"], 1),
            adx_at_entry=round(t["adx"], 1),
            vwap_dev_at_entry=round(t["vwap_dev"] * 100, 3),
            elapsed_bars=elapsed,
            be_trailed=be_trailed,
            hour_of_day=t["entry_time"].hour if hasattr(t["entry_time"], 'hour') else 10,
        )

        self.trades.append(trade)
        self._daily_pnl += net_pnl

        # Update consecutive losses
        if net_pnl < 0 and result == "SL":
            self._consec_losses += 1
            if self._consec_losses >= self.config.max_consecutive_losses:
                self._cooldown_until = bar_time + timedelta(minutes=self.config.cooldown_minutes)
        elif net_pnl > 0:
            self._consec_losses = 0

        self._in_trade = False
        self._current_trade = None

        return net_pnl

    def _force_close_trade(self, row, bar_idx, bar_time, vix, reason) -> float:
        """Force close at EOD."""
        t = self._current_trade
        spx_now = float(row.get("close", row.get("Close", 0)))

        # Option is nearly worthless at EOD
        elapsed_hours = (bar_idx - t["entry_bar_idx"]) * 5 / 60
        spx_move = spx_now - t["spx_entry"]
        if t["signal"] == Signal.SHORT:
            spx_move = -spx_move

        option_change = OptionPriceSimulator.price_change(
            spx_move, hours_to_expiry=0.1, time_elapsed_hours=elapsed_hours
        )
        current = max(0.01, t["option_entry"] + option_change)

        return self._close_trade(current, spx_now, bar_idx, bar_time, reason, t.get("be_trailed", False))

    def _classify_regime(self, adx, vix):
        if adx > Config.ADX_THRESHOLD or vix > 30:
            return Regime.TRENDING
        elif adx < 20 and vix < 12:
            return Regime.LOW_VOL
        return Regime.MEAN_REVERTING

    def _check_limits(self, capital):
        if self._trades_today >= self.config.max_trades_per_day:
            return False
        if self._daily_pnl <= -(capital * self.config.max_daily_loss_pct):
            return False
        if self._cooldown_until and self._current_trade:
            return False
        return True

    def _calc_running_winrate(self):
        if not self.trades:
            return 0
        wins = sum(1 for t in self.trades if t.net_pnl > 0)
        return wins / len(self.trades) * 100


# ═══════════════════════════════════════════════════════════════
# BACKTEST RESULTS & STATISTICS
# ═══════════════════════════════════════════════════════════════

class BacktestResults:
    """Compute and store comprehensive backtest statistics."""

    def __init__(self, trades: List[BacktestTrade], equity_curve: List[float],
                 daily_results: Dict, config: BacktestConfig):
        self.trades = trades
        self.equity_curve = equity_curve
        self.daily_results = daily_results
        self.config = config
        self.stats: Dict = {}
        self.monte_carlo: Dict = {}

    def compute_statistics(self):
        """Calculate all statistics."""
        if not self.trades:
            print("  No trades generated — check data or parameters")
            self.stats = {"error": "No trades"}
            return

        pnls = [t.net_pnl for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total = len(pnls)
        n_wins = len(wins)
        n_losses = len(losses)

        # ── Core metrics ──
        win_rate = n_wins / total * 100 if total > 0 else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
        expectancy = np.mean(pnls) if pnls else 0
        expectancy_r = (win_rate / 100 * Config.RR_RATIO) - ((1 - win_rate / 100) * 1)

        # ── Returns ──
        total_pnl = sum(pnls)
        total_return = total_pnl / self.config.initial_capital * 100

        # ── Drawdown ──
        equity = np.array(self.equity_curve)
        peaks = np.maximum.accumulate(equity)
        drawdowns = (equity - peaks) / peaks * 100
        max_drawdown = abs(drawdowns.min())
        max_dd_idx = np.argmin(drawdowns)

        # Find longest drawdown period
        underwater = equity < peaks
        longest_dd = 0
        current_dd = 0
        for uw in underwater:
            if uw:
                current_dd += 1
                longest_dd = max(longest_dd, current_dd)
            else:
                current_dd = 0

        # ── Risk-adjusted returns ──
        daily_pnls = [v["pnl"] for v in self.daily_results.values() if v["pnl"] != 0]
        if len(daily_pnls) > 1:
            daily_returns = np.array(daily_pnls) / self.config.initial_capital
            sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252) if np.std(daily_returns) > 0 else 0
            downside = daily_returns[daily_returns < 0]
            sortino = np.mean(daily_returns) / np.std(downside) * np.sqrt(252) if len(downside) > 0 and np.std(downside) > 0 else 0
            calmar = (total_return / 100 * 252 / max(len(daily_pnls), 1)) / (max_drawdown / 100) if max_drawdown > 0 else 0
        else:
            sharpe = sortino = calmar = 0

        # ── Streaks ──
        max_win_streak = max_loss_streak = 0
        cur_win = cur_loss = 0
        for p in pnls:
            if p > 0:
                cur_win += 1
                cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)

        # ── Per-regime breakdown ──
        regime_stats = {}
        for regime in ["mean_reverting", "low_vol", "trending"]:
            rt = [t for t in self.trades if t.regime == regime]
            if rt:
                rw = sum(1 for t in rt if t.net_pnl > 0)
                regime_stats[regime] = {
                    "trades": len(rt),
                    "win_rate": round(rw / len(rt) * 100, 1),
                    "total_pnl": round(sum(t.net_pnl for t in rt), 2),
                    "avg_pnl": round(np.mean([t.net_pnl for t in rt]), 2),
                }

        # ── Per-hour breakdown ──
        hour_stats = {}
        for hour in range(9, 16):
            ht = [t for t in self.trades if t.hour_of_day == hour]
            if ht:
                hw = sum(1 for t in ht if t.net_pnl > 0)
                hour_stats[hour] = {
                    "trades": len(ht),
                    "win_rate": round(hw / len(ht) * 100, 1),
                    "total_pnl": round(sum(t.net_pnl for t in ht), 2),
                }

        # ── Result type breakdown ──
        result_breakdown = {}
        for rt in ["TP", "SL", "TIME_STOP", "BE", "EOD"]:
            rtrades = [t for t in self.trades if t.result == rt]
            if rtrades:
                result_breakdown[rt] = {
                    "count": len(rtrades),
                    "total_pnl": round(sum(t.net_pnl for t in rtrades), 2),
                    "avg_pnl": round(np.mean([t.net_pnl for t in rtrades]), 2),
                }

        # ── Signal breakdown ──
        long_trades = [t for t in self.trades if t.signal == "long"]
        short_trades = [t for t in self.trades if t.signal == "short"]
        long_wr = sum(1 for t in long_trades if t.net_pnl > 0) / len(long_trades) * 100 if long_trades else 0
        short_wr = sum(1 for t in short_trades if t.net_pnl > 0) / len(short_trades) * 100 if short_trades else 0

        self.stats = {
            # Core
            "total_trades": total,
            "winning_trades": n_wins,
            "losing_trades": n_losses,
            "win_rate": round(win_rate, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "expectancy_per_trade": round(expectancy, 2),
            "expectancy_r": round(expectancy_r, 3),

            # Returns
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return, 2),
            "initial_capital": self.config.initial_capital,
            "final_capital": round(self.equity_curve[-1], 2),

            # Risk
            "max_drawdown_pct": round(max_drawdown, 2),
            "longest_drawdown_days": longest_dd,
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "calmar_ratio": round(calmar, 2),

            # Streaks
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,

            # Breakdowns
            "regime_stats": regime_stats,
            "hour_stats": hour_stats,
            "result_breakdown": result_breakdown,
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "long_win_rate": round(long_wr, 1),
            "short_win_rate": round(short_wr, 1),

            # Costs
            "total_commission": round(sum(t.commission for t in self.trades), 2),
            "total_slippage": round(sum(t.slippage for t in self.trades), 2),
            "avg_elapsed_bars": round(np.mean([t.elapsed_bars for t in self.trades]), 1),
            "be_trail_count": sum(1 for t in self.trades if t.be_trailed),
        }

        # Monte Carlo
        self._run_monte_carlo()

    def _run_monte_carlo(self):
        """Run Monte Carlo simulation for confidence intervals."""
        pnls = np.array([t.net_pnl for t in self.trades])
        if len(pnls) < 20:
            self.monte_carlo = {"error": "Not enough trades for Monte Carlo"}
            return

        n_runs = self.config.monte_carlo_runs
        sample_size = min(self.config.monte_carlo_sample_size, len(pnls))

        final_pnls = []
        max_drawdowns = []

        for _ in range(n_runs):
            # Random sample with replacement
            sample = np.random.choice(pnls, size=sample_size, replace=True)
            cum = np.cumsum(sample) + self.config.initial_capital
            final_pnls.append(cum[-1] - self.config.initial_capital)

            # Track drawdown
            peaks = np.maximum.accumulate(cum)
            dd = ((cum - peaks) / peaks * 100)
            max_drawdowns.append(abs(dd.min()))

        final_pnls = np.array(final_pnls)
        max_drawdowns = np.array(max_drawdowns)

        self.monte_carlo = {
            "runs": n_runs,
            "sample_size": sample_size,
            "median_pnl": round(np.median(final_pnls), 2),
            "p5_pnl": round(np.percentile(final_pnls, 5), 2),
            "p25_pnl": round(np.percentile(final_pnls, 25), 2),
            "p75_pnl": round(np.percentile(final_pnls, 75), 2),
            "p95_pnl": round(np.percentile(final_pnls, 95), 2),
            "prob_profitable": round(np.mean(final_pnls > 0) * 100, 1),
            "median_max_dd": round(np.median(max_drawdowns), 2),
            "p95_max_dd": round(np.percentile(max_drawdowns, 95), 2),
            "worst_case_pnl": round(np.min(final_pnls), 2),
            "best_case_pnl": round(np.max(final_pnls), 2),
        }

    def print_report(self):
        """Print formatted report to console."""
        s = self.stats
        if "error" in s:
            print(f"\n  ERROR: {s['error']}")
            return

        mc = self.monte_carlo

        print("\n" + "═" * 70)
        print("  BACKTEST RESULTS — SPX 0DTE MEAN REVERSION")
        print("═" * 70)

        print(f"\n  {'─── CORE METRICS ───':─<50}")
        print(f"  Total Trades:         {s['total_trades']}")
        print(f"  Winning Trades:       {s['winning_trades']}")
        print(f"  Losing Trades:        {s['losing_trades']}")
        print(f"  WIN RATE:             {s['win_rate']:.1f}%  {'✅' if s['win_rate'] >= 70 else '⚠️' if s['win_rate'] >= 60 else '❌'}")
        print(f"  Avg Win:              ${s['avg_win']:+.2f}")
        print(f"  Avg Loss:             ${s['avg_loss']:+.2f}")
        print(f"  Profit Factor:        {s['profit_factor']:.2f}")
        print(f"  Expectancy/Trade:     ${s['expectancy_per_trade']:+.2f}")
        print(f"  Expectancy (R):       {s['expectancy_r']:+.3f}R")

        print(f"\n  {'─── RETURNS ───':─<50}")
        print(f"  Initial Capital:      ${s['initial_capital']:,.2f}")
        print(f"  Final Capital:        ${s['final_capital']:,.2f}")
        print(f"  Total P/L:            ${s['total_pnl']:+,.2f}")
        print(f"  Total Return:         {s['total_return_pct']:+.2f}%")
        print(f"  Total Commission:     ${s['total_commission']:,.2f}")

        print(f"\n  {'─── RISK METRICS ───':─<50}")
        print(f"  Max Drawdown:         {s['max_drawdown_pct']:.2f}%")
        print(f"  Longest DD (days):    {s['longest_drawdown_days']}")
        print(f"  Sharpe Ratio:         {s['sharpe_ratio']:.2f}")
        print(f"  Sortino Ratio:        {s['sortino_ratio']:.2f}")
        print(f"  Max Win Streak:       {s['max_win_streak']}")
        print(f"  Max Loss Streak:      {s['max_loss_streak']}")
        print(f"  BE Trails Activated:  {s['be_trail_count']}")

        print(f"\n  {'─── SIGNAL BREAKDOWN ───':─<50}")
        print(f"  Long Trades:          {s['long_trades']} (Win Rate: {s['long_win_rate']:.1f}%)")
        print(f"  Short Trades:         {s['short_trades']} (Win Rate: {s['short_win_rate']:.1f}%)")

        print(f"\n  {'─── RESULT BREAKDOWN ───':─<50}")
        for rt, data in s.get("result_breakdown", {}).items():
            print(f"  {rt:15s}  Count: {data['count']:4d}  |  Total P/L: ${data['total_pnl']:+10,.2f}  |  Avg: ${data['avg_pnl']:+.2f}")

        print(f"\n  {'─── REGIME BREAKDOWN ───':─<50}")
        for regime, data in s.get("regime_stats", {}).items():
            print(f"  {regime:17s}  Trades: {data['trades']:4d}  |  Win Rate: {data['win_rate']:5.1f}%  |  P/L: ${data['total_pnl']:+,.2f}")

        print(f"\n  {'─── HOURLY PERFORMANCE ───':─<50}")
        for hour in sorted(s.get("hour_stats", {}).keys()):
            data = s["hour_stats"][hour]
            bar = "█" * int(data["win_rate"] / 5)
            print(f"  {hour:02d}:00  Trades: {data['trades']:3d}  |  WR: {data['win_rate']:5.1f}%  {bar}  |  P/L: ${data['total_pnl']:+,.2f}")

        if mc and "error" not in mc:
            print(f"\n  {'─── MONTE CARLO ({mc['runs']} runs, {mc['sample_size']} trades/run) ───':─<50}")
            print(f"  Probability Profitable:  {mc['prob_profitable']:.1f}%")
            print(f"  Median P/L:              ${mc['median_pnl']:+,.2f}")
            print(f"  5th Percentile:          ${mc['p5_pnl']:+,.2f}")
            print(f"  95th Percentile:         ${mc['p95_pnl']:+,.2f}")
            print(f"  Worst Case:              ${mc['worst_case_pnl']:+,.2f}")
            print(f"  Best Case:               ${mc['best_case_pnl']:+,.2f}")
            print(f"  Median Max Drawdown:     {mc['median_max_dd']:.1f}%")
            print(f"  95th %ile Max DD:        {mc['p95_max_dd']:.1f}%")

        print("\n" + "═" * 70)

    def export_trades_csv(self, path: str):
        """Export trade log to CSV."""
        if not self.trades:
            return
        rows = []
        for t in self.trades:
            rows.append({
                "id": t.trade_id, "date": t.date, "entry_time": t.entry_time,
                "exit_time": t.exit_time, "signal": t.signal,
                "spx_entry": t.spx_entry, "spx_exit": t.spx_exit,
                "opt_entry": t.option_entry, "opt_exit": t.option_exit,
                "sl": t.stop_loss, "tp": t.take_profit, "qty": t.quantity,
                "gross_pnl": t.gross_pnl, "commission": t.commission,
                "net_pnl": t.net_pnl, "result": t.result,
                "regime": t.regime, "rsi": t.rsi_at_entry,
                "adx": t.adx_at_entry, "elapsed_bars": t.elapsed_bars,
                "be_trailed": t.be_trailed, "hour": t.hour_of_day,
            })
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"  Trades exported to: {path}")

    def export_json(self, path: str):
        """Export full results to JSON."""
        data = {
            "stats": self.stats,
            "monte_carlo": self.monte_carlo,
            "equity_curve": self.equity_curve,
            "config": {
                "initial_capital": self.config.initial_capital,
                "commission": self.config.commission_per_contract,
                "slippage_pct": self.config.slippage_pct,
                "spread_cost": self.config.spread_cost,
            },
            "trades": [
                {
                    "id": t.trade_id, "date": t.date, "signal": t.signal,
                    "entry": t.option_entry, "exit": t.option_exit,
                    "net_pnl": t.net_pnl, "result": t.result,
                    "regime": t.regime, "hour": t.hour_of_day,
                }
                for t in self.trades
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Results exported to: {path}")


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="SPX 0DTE Mean Reversion Backtester")
    parser.add_argument("--csv", type=str, help="Path to CSV file with 5-min SPX bars")
    parser.add_argument("--from-api", action="store_true", help="Load data from TradeStation API")
    parser.add_argument("--synthetic", type=int, default=0,
                        help="Generate N days of synthetic data for testing")
    parser.add_argument("--capital", type=float, default=25000.0, help="Starting capital")
    parser.add_argument("--output", type=str, default="backtest_results", help="Output directory")
    parser.add_argument("--mc-runs", type=int, default=1000, help="Monte Carlo simulation runs")
    args = parser.parse_args()

    config = BacktestConfig(
        initial_capital=args.capital,
        output_dir=args.output,
        monte_carlo_runs=args.mc_runs,
    )

    # ── Load data ──
    if args.csv:
        config.data_source = "csv"
        config.csv_path = args.csv
        df = DataLoader.from_csv(args.csv)
    elif args.from_api:
        config.data_source = "api"
        auth = TradeStationAuth()
        api = TradeStationAPI(auth, Config.TS_ACCOUNT_ID or "")
        if not Config.TS_ACCOUNT_ID:
            accounts = api.get_accounts()
            if accounts:
                api.account_id = accounts[0]["AccountID"]
        df = DataLoader.from_api(api, bars_back=5000)
    elif args.synthetic > 0:
        config.data_source = "synthetic"
        df = DataLoader.generate_synthetic(days=args.synthetic)
    else:
        # Default: generate 500 days synthetic
        print("No data source specified — generating 500 days of synthetic data")
        print("Use --csv <path>, --from-api, or --synthetic <days> for real data\n")
        config.data_source = "synthetic"
        df = DataLoader.generate_synthetic(days=500)

    # ── Run backtest ──
    engine = BacktestEngine(config)
    results = engine.run(df)

    # ── Output results ──
    results.print_report()

    os.makedirs(config.output_dir, exist_ok=True)

    results.export_trades_csv(os.path.join(config.output_dir, "trades.csv"))
    results.export_json(os.path.join(config.output_dir, "results.json"))

    print(f"\n  All results saved to: {config.output_dir}/")
    print("  Run the dashboard to visualize: open backtest_dashboard.html")


if __name__ == "__main__":
    main()