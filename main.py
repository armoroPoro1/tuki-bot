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
from src.trading.strategy import ohlcv_to_dataframe, strategy_signal


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


def compute_profit_loss_pct(*, side: str, entry_price: float, exit_price: float) -> float:
    """
    Profit/Loss in percent of entry:
    - long: (exit - entry) / entry * 100
    - short: (entry - exit) / entry * 100
    """
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
    """
    Normalize a symbol into `BASE/QUOTE` format used by ccxt.

    Accepts:
    - `BTC/USDT`
    - `BTCUSDT`
    """
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

    # Binance API credentials
    # (Multi-symbol config comes from STRATEGIES_CONFIG, not BINANCE_SYMBOL)

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET_KEY")
    if not api_key or not api_secret:
        raise RuntimeError("Missing BINANCE_API_KEY/BINANCE_SECRET_KEY in .env")

    # ----------------------------
    # 2) Optional: Google Sheets logger
    # ----------------------------
    # Enable by providing both:
    # - GOOGLE_SHEETS_JSON_KEY (service account JSON path)
    # - GOOGLE_SHEET_NAME
    gs_json_path = _env_str("GOOGLE_SHEETS_JSON_KEY", "")
    sheet_name = _env_str("GOOGLE_SHEET_NAME", "")
    worksheet_name = _env_str("GOOGLE_WORKSHEET_NAME", None)

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
            # If Sheets init fails (network/permissions), don't block trading.
            print("Google Sheets initialization failed:", repr(e))
            logger = None

    # ----------------------------
    # 3) Trading parameters
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
    # 4) Setup Binance exchange
    # ----------------------------
    exchange = create_binance_futures_exchange(api_key, api_secret, testnet=testnet)

    if not STRATEGIES_CONFIG:
        # Fallback to old single-symbol flow if you ever remove config.
        symbol = _env_str("BINANCE_SYMBOL", "BTC/USDT")
        symbols_config = {symbol: []}
        raise RuntimeError("STRATEGIES_CONFIG is empty; single-symbol fallback is not implemented.")
    else:
        # Normalize keys to ccxt symbol format: `BASE/QUOTE` (e.g. BTC/USDT)
        symbols_config: dict[str, list[dict[str, object]]] = {}
        for raw_symbol, strategies in STRATEGIES_CONFIG.items():
            norm = normalize_binance_symbol(raw_symbol, quote="USDT")
            # Keep strategy dict as-is; we only normalize symbol keys.
            symbols_config[norm] = list(strategies)

    symbols_to_trade = list(symbols_config.keys())

    exchange.load_markets()
    missing = [s for s in symbols_to_trade if s not in exchange.markets]
    if missing:
        raise ValueError(f"Symbols not found on exchange: {missing}. Check spelling/quote currency.")

    # Position managers handle leverage + open/close orders (one per symbol).
    pos_mgrs = {symbol: PositionManager(exchange, symbol=symbol, leverage=leverage) for symbol in symbols_to_trade}

    ohlcv_limit = 100

    # Track currently-open trade metadata so we can log TP/SL closes
    # that happen on the exchange-side (without calling close_position()).
    open_trade_meta: dict[str, dict[str, Any]] = {}

    print(
        "Bot started on Binance Futures testnet "
        f"(timeframe={timeframe}, symbols={', '.join(symbols_to_trade)})"
    )
    print("Loop interval:", loop_seconds, "seconds")

    while True:
        try:
            print(f"Loop tick: checking {len(symbols_to_trade)} symbols...")
            if logger:
                logger.update_dashboard_last_update_time()
            balance_usdt: float | None = None

            # ----------------------------
            # 5) Per-symbol evaluate + open/close
            # ----------------------------
            for symbol in symbols_to_trade:
                pos_mgr = pos_mgrs[symbol]
                try:
                    current_position = pos_mgr.get_open_position()

                    # If the position got closed already (e.g. TP/SL triggered on
                    # the exchange-side), log it using our saved entry + order ids.
                    if current_position is None and symbol in open_trade_meta:
                        meta = open_trade_meta[symbol]
                        entry = float(meta["entry_price"])
                        side = str(meta["side"])  # "long" | "short"
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
                            f"Closed {side.upper()} {symbol} at {exit_price} "
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
                    # Open position (if none)
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

                            sig = strategy_signal(df=df, strategy_type=strategy_type, params=params)
                            if sig is None:
                                continue

                            if alloc > best_alloc:
                                best_alloc = alloc
                                best_candidate = (sig, strat)

                        if best_candidate is None:
                            continue

                        sig, strat = best_candidate
                        allocation = float(strat.get("allocation", 1.0) or 0.0)
                        strategy_id = str(strat.get("strategy_id", strat.get("type", "UNKNOWN")))

                        if balance_usdt is None:
                            balance_usdt = get_usdt_balance(exchange)

                        # Scale risk by allocation.
                        risk_effective_pct = risk_per_trade_pct * allocation

                        sizing = calculate_position_size(
                            balance_usdt=balance_usdt,
                            entry_price=last,
                            leverage=leverage,
                            risk_per_trade_pct=risk_effective_pct,
                            # calculate_position_size expects stop_loss_pct as percent (e.g. 1 for 1%)
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
                            f"Opened {sig.side.upper()} {symbol} via {strategy_id} "
                            f"(alloc={allocation:.3f}) qty={qty} entry≈{opened.entry_price}. Waiting for TP/SL..."
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
                    # Exit logic (TP/SL) for an open position
                    # ----------------------------
                    if getattr(current_position, "brackets_placed", False):
                        # TP/SL were placed as exchange-side conditional orders.
                        # Let Binance handle it; manual TP/SL fallback only applies if brackets weren't placed.
                        continue

                    entry = float(current_position.entry_price)
                    side = current_position.side

                    take_profit_price = entry * (1.0 + take_profit_pct) if side == "long" else entry * (1.0 - take_profit_pct)
                    stop_loss_price = entry * (1.0 - stop_loss_pct) if side == "long" else entry * (1.0 + stop_loss_pct)

                    should_take_profit = last >= take_profit_price if side == "long" else last <= take_profit_price
                    should_stop_loss = last <= stop_loss_price if side == "long" else last >= stop_loss_price

                    if should_take_profit or should_stop_loss:
                        exit_price, _order_raw = pos_mgr.close_position(current_position)
                        pl_pct = compute_profit_loss_pct(side=side, entry_price=entry, exit_price=exit_price)
                        meta = open_trade_meta.get(symbol)
                        strategy_id = str(meta["strategy_id"]) if meta else "UNKNOWN"

                        print(
                            f"Closed {side.upper()} {symbol} at {exit_price} "
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

                        # Clear meta so we don't log again next tick.
                        if symbol in open_trade_meta:
                            del open_trade_meta[symbol]

                except Exception as e:
                    print(f"[{symbol}] symbol loop error:", repr(e))
                    continue

            safe_sleep(loop_seconds)

        # ----------------------------
        # 9) Error handling (network/API)
        # ----------------------------
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.ExchangeError) as e:
            print("Network/API connectivity error:", repr(e), "-> sleeping before retry")
            safe_sleep(30)
        except ccxt.BaseError as e:
            print("ccxt error:", repr(e), "-> sleeping before retry")
            safe_sleep(15)
        except Exception as e:
            print("Unexpected error:", repr(e), "-> sleeping before retry")
            safe_sleep(15)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped by user.")
        sys.exit(0)

