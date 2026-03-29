"""
main2.py — Counter-trade bot (สวนสัญญาณ)

ทำงานเหมือน main.py ทุกอย่าง ยกเว้น:
  - ใช้ BINANCE_API_KEY_2 / BINANCE_SECRET_KEY_2
  - บันทึกผลใน GOOGLE_WORKSHEET_NAME_2
  - กลับทิศทาง signal ทุกตัว (long → short, short → long)
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Optional

import ccxt
from dotenv import load_dotenv

from src.config.strategies_config import STRATEGIES_CONFIG
from src.exchange.binance_futures import create_binance_futures_exchange
from src.sheets.google_sheets_service import GoogleSheetsLogger
from src.trading.position_manager import PositionManager
from src.trading.risk import calculate_position_size, get_usdt_balance, quantize_quantity
from src.trading.strategy import Signal, ohlcv_to_dataframe, strategy_signal

BOT_LABEL = "[BOT-2/COUNTER]"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw


def invert_signal(sig: Signal) -> Signal:
    """กลับทิศสัญญาณ: long → short, short → long"""
    return Signal(side="short" if sig.side == "long" else "long")


def compute_profit_loss_pct(*, side: str, entry_price: float, exit_price: float) -> float:
    entry_price = float(entry_price)
    exit_price = float(exit_price)
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    if side == "long":
        return (exit_price - entry_price) / entry_price * 100.0
    if side == "short":
        return (entry_price - exit_price) / entry_price * 100.0
    raise ValueError("side must be 'long' or 'short'")


def safe_sleep(seconds: int) -> None:
    time.sleep(max(1, int(seconds)))


def normalize_binance_symbol(raw_symbol: str, *, quote: str = "USDT") -> str:
    s = (raw_symbol or "").strip().upper()
    if not s:
        raise ValueError("raw_symbol is empty")
    if "/" in s:
        return s
    if quote and s.endswith(quote) and len(s) > len(quote):
        base = s[: -len(quote)]
        return f"{base}/{quote}"
    raise ValueError(f"Unsupported symbol format: {raw_symbol!r} (expected {quote} quote)")


def main() -> None:
    # ----------------------------
    # 1) Load config (.env)
    # ----------------------------
    load_dotenv()

    # ใช้ API key ชุดที่ 2
    api_key = os.getenv("BINANCE_API_KEY_2")
    api_secret = os.getenv("BINANCE_SECRET_KEY_2")
    if not api_key or not api_secret:
        raise RuntimeError(f"{BOT_LABEL} Missing BINANCE_API_KEY_2/BINANCE_SECRET_KEY_2 in .env")

    # ----------------------------
    # 2) Optional: Google Sheets logger (worksheet ชุดที่ 2)
    # ----------------------------
    gs_json_path = _env_str("GOOGLE_SHEETS_JSON_KEY", "")
    sheet_name = _env_str("GOOGLE_SHEET_NAME", "")
    worksheet_name = _env_str("GOOGLE_WORKSHEET_NAME_2", None)  # ← ต่างจาก main.py

    enable_google_sheets = bool(gs_json_path and sheet_name)
    logger = None
    if enable_google_sheets:
        try:
            logger = GoogleSheetsLogger(
                service_account_json_path=gs_json_path,
                sheet_name=sheet_name,
                worksheet_name=worksheet_name,
            )
        except Exception as e:
            print(f"{BOT_LABEL} Google Sheets initialization failed:", repr(e))
            logger = None

    # ----------------------------
    # 3) Trading parameters (อ่านค่าเดิมจาก .env เหมือน bot หลัก)
    # ----------------------------
    timeframe = os.getenv("TIMEFRAME", "1h")
    loop_seconds = _env_int("LOOP_SECONDS", 60)

    leverage = _env_int("LEVERAGE", 5)
    risk_per_trade_pct = _env_float("RISK_PER_TRADE_PCT", 1.0)
    take_profit_pct = _env_float("TAKE_PROFIT_PCT", 2.0) / 100.0
    stop_loss_pct = _env_float("STOP_LOSS_PCT", 1.0) / 100.0
    use_exchange_tp_sl = _env_int("USE_EXCHANGE_TP_SL", 1) == 1

    testnet = True

    # ----------------------------
    # 4) Setup Binance exchange (ด้วย key ชุดที่ 2)
    # ----------------------------
    exchange = create_binance_futures_exchange(api_key, api_secret, testnet=testnet)

    if not STRATEGIES_CONFIG:
        raise RuntimeError(f"{BOT_LABEL} STRATEGIES_CONFIG is empty.")

    symbols_config: dict[str, list[dict[str, object]]] = {}
    for raw_symbol, strategies in STRATEGIES_CONFIG.items():
        norm = normalize_binance_symbol(raw_symbol, quote="USDT")
        symbols_config[norm] = list(strategies)

    symbols_to_trade = list(symbols_config.keys())

    exchange.load_markets()
    missing = [s for s in symbols_to_trade if s not in exchange.markets]
    if missing:
        raise ValueError(f"{BOT_LABEL} Symbols not found on exchange: {missing}")

    pos_mgrs = {
        symbol: PositionManager(exchange, symbol=symbol, leverage=leverage)
        for symbol in symbols_to_trade
    }

    ohlcv_limit = 100
    open_trade_meta: dict[str, dict[str, Any]] = {}

    print(
        f"{BOT_LABEL} Counter-trade bot started on Binance Futures testnet "
        f"(timeframe={timeframe}, symbols={', '.join(symbols_to_trade)})"
    )
    print(f"{BOT_LABEL} Worksheet: {worksheet_name}")
    print(f"{BOT_LABEL} Loop interval: {loop_seconds} seconds")
    print(f"{BOT_LABEL} *** ALL SIGNALS ARE INVERTED (long→short, short→long) ***")

    while True:
        try:
            print(f"{BOT_LABEL} Loop tick: checking {len(symbols_to_trade)} symbols...")
            if logger:
                logger.update_dashboard_last_update_time("dashboard2")
            balance_usdt: float | None = None

            # ----------------------------
            # 5) Per-symbol evaluate + open/close
            # ----------------------------
            for symbol in symbols_to_trade:
                pos_mgr = pos_mgrs[symbol]
                try:
                    current_position = pos_mgr.get_open_position()

                    # ปิดไม้ที่ถูกปิดด้วย TP/SL ของ exchange
                    if current_position is None and symbol in open_trade_meta:
                        meta = open_trade_meta[symbol]
                        entry = float(meta["entry_price"])
                        side = str(meta["side"])
                        strategy_id = str(meta["strategy_id"])

                        tp_order_id = meta.get("tp_order_id")
                        sl_order_id = meta.get("sl_order_id")

                        exit_price: float | None = None
                        if use_exchange_tp_sl:
                            for oid in (tp_order_id, sl_order_id):
                                if not oid:
                                    continue
                                try:
                                    ord_raw = exchange.fetch_order(str(oid), symbol)
                                    status = str(ord_raw.get("status", "")).lower()
                                    avg = ord_raw.get("average") or ord_raw.get("price")
                                    filled_raw = ord_raw.get("filled")
                                    filled_ok = False
                                    if filled_raw is not None:
                                        try:
                                            filled_ok = float(filled_raw) != 0.0
                                        except Exception:
                                            filled_ok = True

                                    if avg is not None and (status in ("closed", "filled") or filled_ok):
                                        exit_price = float(avg)
                                        break
                                except Exception:
                                    continue

                        if exit_price is None:
                            ticker = exchange.fetch_ticker(symbol)
                            exit_price = float(ticker["last"])

                        pl_pct = compute_profit_loss_pct(side=side, entry_price=entry, exit_price=exit_price)

                        print(
                            f"{BOT_LABEL} Closed {side.upper()} {symbol} at {exit_price} "
                            f"(P/L={pl_pct:.3f}%) via exchange TP/SL. strategy={strategy_id}"
                        )

                        if logger:
                            logger.log_close(
                                symbol=symbol,
                                strategy=strategy_id,
                                side="Long" if side == "long" else "Short",
                                entry_price=entry,
                                exit_price=exit_price,
                                profit_loss_pct=pl_pct,
                            )

                        del open_trade_meta[symbol]

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=ohlcv_limit)
                    if not ohlcv or len(ohlcv) < 3:
                        continue

                    df = ohlcv_to_dataframe(ohlcv)
                    last = float(df.iloc[-1]["close"])

                    # ----------------------------
                    # เปิดไม้ (ถ้ายังไม่มี position)
                    # ----------------------------
                    if current_position is None:
                        best_candidate = None
                        best_alloc = -1.0

                        for strat in symbols_config[symbol]:
                            strategy_type = str(strat.get("type", "")).strip()
                            params = strat.get("params", {}) if isinstance(strat.get("params", {}), dict) else {}
                            alloc = float(strat.get("allocation", 1.0) or 0.0)
                            if alloc <= 0:
                                continue

                            raw_sig = strategy_signal(df=df, strategy_type=strategy_type, params=params)
                            if raw_sig is None:
                                continue

                            if alloc > best_alloc:
                                best_alloc = alloc
                                best_candidate = (raw_sig, strat)

                        if best_candidate is None:
                            continue

                        raw_sig, strat = best_candidate
                        # *** กลับ signal ***
                        sig = invert_signal(raw_sig)

                        # # Option B: เปิดเฉพาะ Short เท่านั้น — ถ้า invert แล้วได้ Long ให้ข้าม
                        # if sig.side != "short":
                        #     continue

                        allocation = float(strat.get("allocation", 1.0) or 0.0)
                        strategy_id = str(strat.get("strategy_id", strat.get("type", "UNKNOWN")))

                        if balance_usdt is None:
                            balance_usdt = get_usdt_balance(exchange)

                        risk_effective_pct = risk_per_trade_pct * allocation

                        sizing = calculate_position_size(
                            balance_usdt=balance_usdt,
                            entry_price=last,
                            leverage=leverage,
                            risk_per_trade_pct=risk_effective_pct,
                            stop_loss_pct=stop_loss_pct * 100.0,
                        )

                        qty = quantize_quantity(exchange, symbol, sizing.quantity)
                        if use_exchange_tp_sl:
                            opened = pos_mgr.open_position_bracket(
                                trade_side=sig.side,
                                quantity=qty,
                                take_profit_pct=take_profit_pct,
                                stop_loss_pct=stop_loss_pct,
                            )
                        else:
                            opened = pos_mgr.open_position(trade_side=sig.side, quantity=qty)

                        print(
                            f"{BOT_LABEL} Opened {sig.side.upper()} {symbol} via {strategy_id} "
                            f"(orig={raw_sig.side}, INVERTED) alloc={allocation:.3f} "
                            f"qty={qty} entry≈{opened.entry_price}"
                        )

                        if logger:
                            logger.log_open(
                                symbol=symbol,
                                strategy=strategy_id,
                                side="Long" if opened.side == "long" else "Short",
                                entry_price=opened.entry_price,
                            )

                        open_trade_meta[symbol] = {
                            "strategy_id": strategy_id,
                            "side": sig.side,
                            "entry_price": opened.entry_price,
                            "tp_order_id": getattr(opened, "tp_order_id", None),
                            "sl_order_id": getattr(opened, "sl_order_id", None),
                            "brackets_placed": getattr(opened, "brackets_placed", False),
                        }
                        continue

                    # ----------------------------
                    # Exit logic (TP/SL) — ฝั่ง manual fallback
                    # ----------------------------
                    if getattr(current_position, "brackets_placed", False):
                        continue

                    entry = float(current_position.entry_price)
                    side = current_position.side

                    take_profit_price = (
                        entry * (1.0 + take_profit_pct) if side == "long" else entry * (1.0 - take_profit_pct)
                    )
                    stop_loss_price = (
                        entry * (1.0 - stop_loss_pct) if side == "long" else entry * (1.0 + stop_loss_pct)
                    )

                    should_take_profit = last >= take_profit_price if side == "long" else last <= take_profit_price
                    should_stop_loss = last <= stop_loss_price if side == "long" else last >= stop_loss_price

                    if should_take_profit or should_stop_loss:
                        exit_price, _order_raw = pos_mgr.close_position(current_position)
                        pl_pct = compute_profit_loss_pct(side=side, entry_price=entry, exit_price=exit_price)
                        meta = open_trade_meta.get(symbol)
                        strategy_id = str(meta["strategy_id"]) if meta else "UNKNOWN"

                        print(
                            f"{BOT_LABEL} Closed {side.upper()} {symbol} at {exit_price} "
                            f"(P/L={pl_pct:.3f}%). TP={should_take_profit}, SL={should_stop_loss}"
                        )

                        if logger:
                            logger.log_close(
                                symbol=symbol,
                                strategy=strategy_id,
                                side="Long" if side == "long" else "Short",
                                entry_price=entry,
                                exit_price=exit_price,
                                profit_loss_pct=pl_pct,
                            )

                        if symbol in open_trade_meta:
                            del open_trade_meta[symbol]

                except Exception as e:
                    print(f"{BOT_LABEL} [{symbol}] symbol loop error:", repr(e))
                    continue

            safe_sleep(loop_seconds)

        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.ExchangeError) as e:
            print(f"{BOT_LABEL} Network/API error:", repr(e), "-> sleeping before retry")
            safe_sleep(30)
        except ccxt.BaseError as e:
            print(f"{BOT_LABEL} ccxt error:", repr(e), "-> sleeping before retry")
            safe_sleep(15)
        except Exception as e:
            print(f"{BOT_LABEL} Unexpected error:", repr(e), "-> sleeping before retry")
            safe_sleep(15)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"{BOT_LABEL} Stopped by user.")
        sys.exit(0)
