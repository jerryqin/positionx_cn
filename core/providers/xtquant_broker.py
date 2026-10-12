from __future__ import annotations

from typing import Any

from .models import ProviderHealth


class XtQuantBrokerProvider:
    """Read-only compatibility adapter around the current MiniQmtBroker."""

    def __init__(self, broker: Any):
        self._broker = broker

    def health(self) -> ProviderHealth:
        connected = bool(getattr(self._broker, "connected", False))
        return ProviderHealth(
            provider="xtquant_broker",
            status="READY" if connected else "UNKNOWN",
            details={"legacy_broker_injected": self._broker is not None, "connected_attr": connected},
        )

    def get_account_snapshot(self) -> Any:
        return self._broker.query_account()

    def get_positions(self) -> Any:
        return self._broker.query_positions()

    def get_orders(self, query: Any = None) -> Any:
        if query not in (None, {}):
            raise ValueError("LEGACY_BROKER_QUERY_FILTER_NOT_SUPPORTED")
        return self._broker.query_orders()

    def get_trades(self, query: Any = None) -> Any:
        if query not in (None, {}):
            raise ValueError("LEGACY_BROKER_QUERY_FILTER_NOT_SUPPORTED")
        return self._broker.query_trades()
