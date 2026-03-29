from __future__ import annotations

import ccxt


def create_binance_futures_exchange(
    api_key: str,
    api_secret: str,
    *,
    testnet: bool = True,
) -> ccxt.Exchange:
    """
    Create ccxt Binance exchange configured for USDT-margined Futures.

    Notes:
    - Must use Binance Futures Testnet API keys when testnet=True.
    - Newer ccxt versions no longer support futures trade/account calls
      via `set_sandbox_mode(True)`. Instead we manually swap futures URLs to
      Binance Futures Testnet endpoints.
    """
    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
    )

    if testnet:
        # Switch REST endpoints (fapi/dapi) and also SAPI capital/account endpoints.
        # This is required for authenticated calls like fetch_balance.
        test_base = "https://testnet.binancefuture.com"

        if "test" in exchange.urls and "api" in exchange.urls and isinstance(exchange.urls["api"], dict):
            # exchange.urls['test'] contains fapi/dapi endpoints
            for k, v in exchange.urls["test"].items():
                exchange.urls["api"][k] = v

        # For authenticated futures/account endpoints, ccxt fetches from sapi.
        api_urls = exchange.urls.get("api")
        if isinstance(api_urls, dict):
            api_urls["sapi"] = f"{test_base}/sapi/v1"
            api_urls["sapiV2"] = f"{test_base}/sapi/v2"
            api_urls["sapiV3"] = f"{test_base}/sapi/v3"
            api_urls["sapiV4"] = f"{test_base}/sapi/v4"

    return exchange


def set_futures_leverage(
    exchange: ccxt.Exchange,
    symbol: str,
    leverage: int,
) -> None:
    """
    Set leverage for a given futures symbol.
    """
    if hasattr(exchange, "set_leverage"):
        try:
            exchange.set_leverage(leverage, symbol)
            return
        except Exception:
            # Fallback below for ccxt versions that don't implement set_leverage properly.
            pass

    # Binance-specific fallback (ccxt private endpoint naming).
    # If it fails, let the exception bubble up so user sees the root cause.
    if not hasattr(exchange, "fapiPrivate_post_leverage"):
        raise RuntimeError("This ccxt Binance version does not support setting futures leverage.")

    exchange.fapiPrivate_post_leverage(
        {"symbol": symbol.replace("/", ""), "leverage": leverage}
    )


def load_market_and_precision(exchange: ccxt.Exchange, symbol: str) -> dict:
    """
    Ensure market metadata is loaded and return it.
    """
    exchange.load_markets()
    if symbol not in exchange.markets:
        raise ValueError(f"Symbol {symbol!r} not found on this exchange. Check spelling/quote currency.")
    return exchange.markets[symbol]

