from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import ccxt


@dataclass(frozen=True)
class OpenPosition:
    side: str  # "long" | "short"
    entry_price: float
    quantity: float  # contract amount in base currency
    raw: dict[str, Any]
    brackets_placed: bool = False  # True if TP/SL were placed as exchange-side orders
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None


class PositionManager:
    """
    Handles:
    - leverage initialization
    - detecting open positions
    - opening/closing positions with market orders
    """

    def __init__(self, exchange: ccxt.Exchange, symbol: str, leverage: int) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.leverage = leverage
        self._brackets_placed: bool = False
        self._tp_order_id: Optional[str] = None
        self._sl_order_id: Optional[str] = None

    def ensure_leverage(self) -> None:
        # Best-effort; some exchanges/accounts may not allow leverage changes via API.
        try:
            from src.exchange.binance_futures import set_futures_leverage

            set_futures_leverage(self.exchange, self.symbol, self.leverage)
        except Exception:
            # Continue; leverage may already be set or may require manual configuration.
            pass

    def get_open_position(self) -> Optional[OpenPosition]:
        """
        Returns currently open position for this symbol (if any), using fetch_positions().
        """
        if not hasattr(self.exchange, "fetch_positions"):
            return None

        positions = self.exchange.fetch_positions([self.symbol])
        for p in positions:
            contracts = float(p.get("contracts") or p.get("positionAmt") or 0.0)
            if abs(contracts) <= 0:
                continue

            # ccxt binance futures usually returns side in lowercase in "side".
            side = p.get("side") or p.get("positionSide")
            if isinstance(side, str):
                side = side.lower()
            else:
                side = "long" if contracts > 0 else "short"

            entry_price = float(p.get("entryPrice") or p.get("entry_price") or 0.0)
            if entry_price <= 0:
                # Fall back to current price if entry is missing (should be rare).
                ticker = self.exchange.fetch_ticker(self.symbol)
                entry_price = float(ticker["last"])

            return OpenPosition(
                side=side if side in ("long", "short") else "long",
                entry_price=entry_price,
                quantity=abs(contracts),
                brackets_placed=bool(self._brackets_placed),
                tp_order_id=self._tp_order_id,
                sl_order_id=self._sl_order_id,
                raw=p,
            )

        # No open position => bracket orders should be considered inactive.
        self._brackets_placed = False
        self._tp_order_id = None
        self._sl_order_id = None
        return None

    def _create_market_order(self, *, side: str, quantity: float, reduce_only: bool) -> dict[str, Any]:
        """
        For Binance one-way mode:
        - open long = BUY
        - close long = SELL (reduce-only)
        - open short = SELL
        - close short = BUY (reduce-only)
        """
        return self.exchange.create_order(
            self.symbol,
            type="market",
            side=side,
            amount=quantity,
            price=None,
            params={"reduceOnly": reduce_only},
        )

    def open_position(self, *, trade_side: str, quantity: float) -> OpenPosition:
        """
        trade_side: "long" or "short"
        """
        return self.open_position_bracket(
            trade_side=trade_side,
            quantity=quantity,
            take_profit_pct=None,
            stop_loss_pct=None,
        )

    def open_position_bracket(
        self,
        *,
        trade_side: str,
        quantity: float,
        take_profit_pct: float | None,
        stop_loss_pct: float | None,
    ) -> OpenPosition:
        """
        Opens a market position, and (optionally) places TP/SL as exchange-side
        conditional orders based on the executed average entry price.

        take_profit_pct / stop_loss_pct must be in decimal form:
          - 2% => 0.02
        """
        if trade_side not in ("long", "short"):
            raise ValueError("trade_side must be 'long' or 'short'")

        order_side = "buy" if trade_side == "long" else "sell"

        self.ensure_leverage()

        order = self._create_market_order(side=order_side, quantity=quantity, reduce_only=False)

        # Get executed average price (best-effort).
        entry_price = None
        try:
            filled = self.exchange.fetch_order(order["id"], self.symbol)
            entry_price = filled.get("average") or filled.get("price")
        except Exception:
            entry_price = None

        if entry_price is None:
            ticker = self.exchange.fetch_ticker(self.symbol)
            entry_price = ticker["last"]

        brackets_placed = False
        tp_order_id: Optional[str] = None
        sl_order_id: Optional[str] = None

        if take_profit_pct is not None and stop_loss_pct is not None:
            if take_profit_pct <= 0 or stop_loss_pct <= 0:
                raise ValueError("take_profit_pct/stop_loss_pct must be positive when provided.")

            # Quantize trigger prices to exchange precision when possible.
            try:
                self.exchange.load_markets()
            except Exception:
                pass

            tp_price = entry_price * (1.0 + float(take_profit_pct)) if trade_side == "long" else entry_price * (1.0 - float(take_profit_pct))
            sl_price = entry_price * (1.0 - float(stop_loss_pct)) if trade_side == "long" else entry_price * (1.0 + float(stop_loss_pct))

            tp_price_q = tp_price
            sl_price_q = sl_price
            if hasattr(self.exchange, "price_to_precision"):
                try:
                    tp_price_q = float(self.exchange.price_to_precision(self.symbol, tp_price))
                    sl_price_q = float(self.exchange.price_to_precision(self.symbol, sl_price))
                except Exception:
                    # If price precision fails, fall back to raw float prices.
                    tp_price_q = tp_price
                    sl_price_q = sl_price

            # In Binance one-way mode:
            # - long open = BUY; long close = SELL (reduceOnly)
            # - short open = SELL; short close = BUY (reduceOnly)
            close_side = "sell" if trade_side == "long" else "buy"

            try:
                tp_order = self.exchange.create_order(
                    self.symbol,
                    type="TAKE_PROFIT_MARKET",
                    side=close_side,
                    amount=float(quantity),
                    price=None,
                    params={"stopPrice": float(tp_price_q), "reduceOnly": True},
                )
                tp_order_id = str(tp_order.get("id"))

                sl_order = self.exchange.create_order(
                    self.symbol,
                    type="STOP_MARKET",
                    side=close_side,
                    amount=float(quantity),
                    price=None,
                    params={"stopPrice": float(sl_price_q), "reduceOnly": True},
                )
                sl_order_id = str(sl_order.get("id"))

                brackets_placed = True
            except Exception:
                # If TP/SL orders fail, caller can fall back to manual TP/SL.
                brackets_placed = False
                tp_order_id = None
                sl_order_id = None

        self._brackets_placed = brackets_placed
        self._tp_order_id = tp_order_id
        self._sl_order_id = sl_order_id

        return OpenPosition(
            side=trade_side,
            entry_price=float(entry_price),
            quantity=float(quantity),
            brackets_placed=brackets_placed,
            tp_order_id=tp_order_id,
            sl_order_id=sl_order_id,
            raw=order,
        )

    def close_position(self, position: OpenPosition) -> tuple[float, dict[str, Any]]:
        """
        Close the given open position with a market order.
        Returns: (exit_price, order_raw)
        """
        # For closing:
        # - long -> sell (reduce-only)
        # - short -> buy (reduce-only)
        order_side = "sell" if position.side == "long" else "buy"

        order = self._create_market_order(side=order_side, quantity=position.quantity, reduce_only=True)

        exit_price = None
        try:
            filled = self.exchange.fetch_order(order["id"], self.symbol)
            exit_price = filled.get("average") or filled.get("price")
        except Exception:
            exit_price = None

        if exit_price is None:
            ticker = self.exchange.fetch_ticker(self.symbol)
            exit_price = ticker["last"]

        # Position is now closed (manually). Treat bracket orders as inactive from our side.
        self._brackets_placed = False
        self._tp_order_id = None
        self._sl_order_id = None

        return float(exit_price), order


