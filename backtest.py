#!/usr/bin/env python3
"""
backtest.py - Simple backtest script for bot1
Simulates walk-forward trading based on src.config.strategies_config and src.trading.strategy.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

import ccxt
import pandas as pd
from dotenv import load_dotenv

from src.config.strategies_config import STRATEGIES_CONFIG
from src.trading.strategy import ohlcv_to_dataframe, strategy_signal

# Optional: use rich for pretty output if installed
try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    console = None

# We need extra candles to warm up EMA200
WARMUP_CANDLES = 200

def normalize_binance_symbol(raw_symbol: str, quote: str = "USDT") -> str:
    s = (raw_symbol or "").strip().upper()
    if "/" in s:
        return s
    if quote and s.endswith(quote) and len(s) > len(quote):
        base = s[: -len(quote)]
        return f"{base}/{quote}"
    return s

def fetch_historical_data(exchange: ccxt.Exchange, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Fetch recent `days` of klines + extra warmup candles"""
    # Approximate candles per day
    tf_minutes = 60 if timeframe == "1h" else 5 if timeframe == "5m" else 15
    candles_per_day = (24 * 60) // tf_minutes
    total_limit = (days * candles_per_day) + WARMUP_CANDLES

    if HAS_RICH:
        console.print(f"  [dim]Fetching {total_limit} candles list for {symbol} ({timeframe})...[/dim]", end=" ")
    else:
        print(f"  Fetching {total_limit} candles list for {symbol} ({timeframe})...", end=" ")

    # ccxt fetch_ohlcv normally limits to 1000 or 1500 per request
    # To get more, we could paginate. But for 30d of 1h (720 candles + 200 = 920), 1 request is enough.
    # If timeframe is smaller, we'd need pagination.
    # For simplicity, we just request using since.
    since = int(exchange.milliseconds() - (total_limit * tf_minutes * 60 * 1000))
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1500)
    
    if HAS_RICH:
        console.print(f"[green]got {len(ohlcv)}[/green]")
    else:
        print(f"got {len(ohlcv)}")

    return ohlcv_to_dataframe(ohlcv)

def simulate_trade(df: pd.DataFrame, entry_idx: int, entry_price: float, side: str, 
                   take_profit_pct: float, stop_loss_pct: float) -> dict[str, Any]:
    """
    Scan forward from entry_idx to see if TP or SL hits first.
    """
    take_profit = entry_price * (1 + take_profit_pct) if side == "long" else entry_price * (1 - take_profit_pct)
    stop_loss = entry_price * (1 - stop_loss_pct) if side == "long" else entry_price * (1 + stop_loss_pct)

    for i in range(entry_idx + 1, len(df)):
        row = df.iloc[i]
        high = float(row["high"])
        low = float(row["low"])
        close_price = float(row["close"])

        tp_hit = (high >= take_profit) if side == "long" else (low <= take_profit)
        sl_hit = (low <= stop_loss) if side == "long" else (high >= stop_loss)

        if sl_hit or tp_hit:
            if sl_hit:
                # pessimistic assumption: SL hits first inside same candle
                exit_price = stop_loss
                reason = "SL"
            else:
                exit_price = take_profit
                reason = "TP"
            
            pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if side == "long" else ((entry_price - exit_price) / entry_price * 100.0)
            
            return {
                "exit_idx": i,
                "exit_price": exit_price,
                "reason": reason,
                "pnl_pct": pnl_pct,
                "exit_time": row["timestamp"],
            }
    
    # Timeout / Still open
    last_idx = len(df) - 1
    last_close = float(df.iloc[-1]["close"])
    pnl_pct = ((last_close - entry_price) / entry_price * 100.0) if side == "long" else ((entry_price - last_close) / entry_price * 100.0)
    return {
        "exit_idx": last_idx,
        "exit_price": last_close,
        "reason": "OPEN",
        "pnl_pct": pnl_pct,
        "exit_time": df.iloc[-1]["timestamp"]
    }

def print_summary(all_trades: dict[str, list[dict]], min_trades: int = 5):
    if HAS_RICH:
        tbl = Table(title="Backtest Summary", show_header=True)
        tbl.add_column("Symbol", justify="left", style="cyan")
        tbl.add_column("Strategy", justify="left")
        tbl.add_column("Trades", justify="right")
        tbl.add_column("Win Rate", justify="right")
        tbl.add_column("Tot PnL%", justify="right")
        
        for symbol, trades in all_trades.items():
            if not trades:
                continue
            
            # Group by strategy
            strats = set(t["strategy"] for t in trades)
            for strat in strats:
                s_trades = [t for t in trades if t["strategy"] == strat]
                closed = [t for t in s_trades if t["reason"] != "OPEN"]
                wins = len([t for t in closed if t["reason"] == "TP"])
                
                win_rate = (wins / len(closed) * 100.0) if closed else 0.0
                total_pnl = sum(t["pnl_pct"] for t in closed)
                
                wr_str = f"{win_rate:.1f}%"
                pnl_str = f"{total_pnl:+.2f}%"
                
                wr_color = "[green]" if win_rate >= 50 else "[red]"
                pnl_color = "[green]" if total_pnl > 0 else "[red]"
                
                tbl.add_row(symbol, strat, str(len(s_trades)), f"{wr_color}{wr_str}[/]", f"{pnl_color}{pnl_str}[/]")
        
        console.print()
        console.print(tbl)
    else:
        print("\n--- Backtest Summary ---")
        print(f"{'Symbol':<15} {'Strategy':<20} {'Trades':<8} {'Win Rate':<10} {'Tot PnL%':<10}")
        for symbol, trades in all_trades.items():
            if not trades:
                continue
            strats = set(t["strategy"] for t in trades)
            for strat in strats:
                s_trades = [t for t in trades if t["strategy"] == strat]
                closed = [t for t in s_trades if t["reason"] != "OPEN"]
                wins = len([t for t in closed if t["reason"] == "TP"])
                win_rate = (wins / len(closed) * 100.0) if closed else 0.0
                total_pnl = sum(t["pnl_pct"] for t in closed)
                
                print(f"{symbol:<15} {strat:<20} {len(s_trades):<8} {win_rate:>8.1f}% {total_pnl:>9.2f}%")

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward Backtest for bot1")
    parser.add_argument("--days", type=int, default=30, help="Days to backtest (default: 30)")
    parser.add_argument("--symbols", nargs="*", help="Override symbols to test")
    args = parser.parse_args()

    load_dotenv()
    
    timeframe = os.getenv("TIMEFRAME", "1h")
    take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "2.0")) / 100.0
    stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "1.0")) / 100.0
    
    if HAS_RICH:
        console.rule("[bold cyan]bot1 Backtester[/bold cyan]")
        console.print(f"Timeframe: {timeframe} | TP: {take_profit_pct*100}% | SL: {stop_loss_pct*100}% | Days: {args.days}")
    else:
        print("Bot 1 Backtester")
        print(f"Timeframe: {timeframe} | TP: {take_profit_pct*100}% | SL: {stop_loss_pct*100}% | Days: {args.days}")

    exchange = ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    })
    
    symbols_to_trade = args.symbols if args.symbols else list(STRATEGIES_CONFIG.keys())
    
    all_trades: dict[str, list[dict]] = {sym: [] for sym in symbols_to_trade}
    
    for raw_symbol in symbols_to_trade:
        symbol = normalize_binance_symbol(raw_symbol)
        
        strategies = STRATEGIES_CONFIG.get(raw_symbol)
        if not strategies:
            if HAS_RICH: console.print(f"[yellow]Skipping {symbol} (no config)[/yellow]")
            continue
            
        try:
            df = fetch_historical_data(exchange, symbol, timeframe, args.days)
        except Exception as e:
            if HAS_RICH: console.print(f"[red]Error fetching {symbol}: {e}[/red]")
            continue
            
        if len(df) < WARMUP_CANDLES + 10:
            if HAS_RICH: console.print(f"[yellow]Not enough data for {symbol}[/yellow]")
            continue

        if HAS_RICH:
            console.print(f"  [cyan]Walking forward {len(df) - WARMUP_CANDLES} candles...[/cyan]")
        else:
            print(f"  Walking forward {len(df) - WARMUP_CANDLES} candles...")
            
        pos_exit_idx = -1
        
        for i in range(WARMUP_CANDLES, len(df)):
            # skip simulation while in position
            if i <= pos_exit_idx:
                continue
                
            window = df.iloc[: i + 1]
            last = window.iloc[-1]
            last_close = float(last["close"])
            
            # Find first signal across configured strategies
            for strat in strategies:
                stype = strat.get("type", "")
                params = strat.get("params", {})
                alloc = strat.get("allocation", 1.0)
                if alloc <= 0:
                    continue
                    
                sig = strategy_signal(df=window, strategy_type=stype, params=params)
                if sig:
                    # Execute trade simulation
                    result = simulate_trade(df, i, last_close, sig.side, take_profit_pct, stop_loss_pct)
                    
                    trade_record = {
                        "strategy": stype,
                        "side": sig.side,
                        "entry_time": last["timestamp"],
                        "entry_price": last_close,
                        "exit_time": result["exit_time"],
                        "exit_price": result["exit_price"],
                        "reason": result["reason"],
                        "pnl_pct": result["pnl_pct"],
                        "exit_idx": result["exit_idx"]
                    }
                    all_trades[raw_symbol].append(trade_record)
                    pos_exit_idx = result["exit_idx"]
                    break # Don't open multiple trades for same candle
                    
        # Print trades for the symbol
        for t in all_trades[raw_symbol]:
            rs_color = "green" if t["pnl_pct"] > 0 else "red"
            tstr = f"[{t['entry_time'].strftime('%m-%d %H:%M')}] {t['side'].upper()} @ {t['entry_price']:.4f} -> {t['reason']} ({t['pnl_pct']:+.2f}%)"
            if HAS_RICH:
                console.print(f"    [{rs_color}]{tstr}[/{rs_color}]")
            else:
                print(f"    {tstr}")
                
    print_summary(all_trades)

if __name__ == "__main__":
    main()
