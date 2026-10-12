from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .broker import BrokerProvider
from .execution import ExecutionProvider
from .market_data import MarketDataProvider
from .xtquant_broker import XtQuantBrokerProvider
from .xtquant_execution import XtQuantExecutionProvider
from .xtquant_market_data import XtQuantMarketDataProvider


@dataclass(frozen=True)
class ProviderBundle:
    market_data: MarketDataProvider
    broker: BrokerProvider
    execution: ExecutionProvider


def build_legacy_xtquant_bundle(
    *,
    market_service: Any,
    broker: Any,
    xtdata_backend: Any = None,
) -> ProviderBundle:
    """Wrap current PositionX runtime objects without constructing or connecting QMT."""
    return ProviderBundle(
        market_data=XtQuantMarketDataProvider(market_service, xtdata_backend=xtdata_backend),
        broker=XtQuantBrokerProvider(broker),
        execution=XtQuantExecutionProvider(broker),
    )
