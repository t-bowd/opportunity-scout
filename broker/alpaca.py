"""
Thin Alpaca Trading API (v2) REST client.

Deliberately small — it wraps only the handful of endpoints the daily run uses
(submit / cancel / list orders, positions, account) with `requests`, rather than
pulling in the `alpaca-py` SDK. Matches the repo's "duplicate a little, don't
drag in a dep" ethos (see smallcap/pricing.py).

Every method raises BrokerError on failure; callers fail soft so a broker hiccup
never blocks the ASX simulator or the snapshot.
"""
from __future__ import annotations

import requests

from broker import config

_TIMEOUT = 15


class BrokerError(RuntimeError):
    """Any non-2xx response or transport failure from Alpaca."""


class AlpacaClient:
    def __init__(self, key: str, secret: str, base_url: str):
        self._base = base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }

    # --- low-level ---------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> dict | list | None:
        url = f"{self._base}{path}"
        try:
            r = requests.request(method, url, headers=self._headers, timeout=_TIMEOUT, **kw)
        except requests.RequestException as e:
            raise BrokerError(f"{method} {path} transport error: {e}") from e
        if r.status_code == 204:
            return None
        if not r.ok:
            raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # --- account / positions ----------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", "/v2/account")

    def get_positions(self) -> list[dict]:
        return self._request("GET", "/v2/positions") or []

    # --- orders ------------------------------------------------------------
    def submit_market_buy(self, symbol: str, qty: int, client_order_id: str) -> dict:
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        })

    def submit_stop_sell(self, symbol: str, qty: int, stop_price: float,
                         client_order_id: str) -> dict:
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": str(round(stop_price, 2)),
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })

    def submit_trailing_stop_sell(self, symbol: str, qty: int, trail_percent: float,
                                  client_order_id: str) -> dict:
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "trailing_stop",
            "trail_percent": str(trail_percent),
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })

    def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> dict:
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        })

    def cancel_order(self, order_id: str) -> None:
        # 404 means it's already gone (filled/cancelled) — not an error for us.
        try:
            self._request("DELETE", f"/v2/orders/{order_id}")
        except BrokerError as e:
            if "404" not in str(e):
                raise

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/orders/{order_id}")

    def list_closed_orders(self, after_iso: str, limit: int = 500) -> list[dict]:
        """Closed orders after an RFC3339 timestamp, oldest first."""
        return self._request("GET", "/v2/orders", params={
            "status": "closed",
            "after": after_iso,
            "limit": limit,
            "direction": "asc",
        }) or []


def client() -> AlpacaClient | None:
    """Env-configured client, or None when the broker is disabled (no keys)."""
    if not config.enabled():
        return None
    return AlpacaClient(config.api_key(), config.secret_key(), config.base_url())
