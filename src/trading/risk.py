from __future__ import annotations

from dataclasses import dataclass

import ccxt


@dataclass(frozen=True)
class RiskSizingResult:
    notional_usdt: float
    margin_usdt: float
    quantity: float  # contract amount in base currency


def get_usdt_balance(exchange: ccxt.Exchange) -> float:
    """
    Best-effort extraction of available USDT balance from ccxt futures balance response.
    """
    balance = exchange.fetch_balance()

    # ccxt usually returns: balance["USDT"] = {"free": ..., "total": ...}
    usdt = balance.get("USDT")
    if isinstance(usdt, dict):
        free = usdt.get("free")
        if free is not None:
            return float(free)
        total = usdt.get("total")
        if total is not None:
            return float(total)

    # Fallbacks
    total = balance.get("total", {}).get("USDT")
    if total is not None:
        return float(total)

    raise RuntimeError("Could not determine USDT balance from exchange.fetch_balance() response.")


def calculate_position_size(
    *,
    balance_usdt: float,
    entry_price: float,
    leverage: int,
    risk_per_trade_pct: float,
    stop_loss_pct: float,
) -> RiskSizingResult:
    """
    Risk rule (1%):
    - Max loss (USDT) = balance * risk_per_trade_pct
    - If stop loss is -stop_loss_pct from entry, then PnL% ~= stop_loss_pct of notional.
    - Therefore notional = max_loss / stop_loss_pct

    Then margin = notional / leverage
    and quantity = notional / entry_price
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be positive.")
    if leverage <= 0:
        raise ValueError("leverage must be positive.")
    if risk_per_trade_pct <= 0:
        raise ValueError("risk_per_trade_pct must be positive.")
    if stop_loss_pct <= 0:
        raise ValueError("stop_loss_pct must be positive.")

    max_loss_usdt = balance_usdt * (risk_per_trade_pct / 100.0)
    notional_usdt = max_loss_usdt / (stop_loss_pct / 100.0)
    margin_usdt = notional_usdt / float(leverage)
    quantity = notional_usdt / entry_price

    return RiskSizingResult(
        notional_usdt=notional_usdt,
        margin_usdt=margin_usdt,
        quantity=quantity,
    )


def quantize_quantity(exchange: ccxt.Exchange, symbol: str, quantity: float) -> float:
    """
    Quantize order quantity according to exchange precision rules.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive.")
    exchange.load_markets()
    market = exchange.market(symbol)
    if not market:
        raise ValueError(f"Market metadata not found for {symbol!r}")

    # ccxt amount_to_precision returns a string; convert back to float.
    return float(exchange.amount_to_precision(symbol, quantity))

