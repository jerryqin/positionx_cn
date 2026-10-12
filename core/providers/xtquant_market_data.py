from __future__ import annotations

from typing import Any, Mapping, Sequence

from .models import BarsRequest, ProviderCapabilityError, ProviderHealth


class XtQuantMarketDataProvider:
    """Compatibility adapter around the current MarketDataService plus optional raw xtdata backend.

    M1B1 does not import xtquant here.  The existing runtime object is injected by M1C.
    """

    def __init__(self, market_service: Any, xtdata_backend: Any = None):
        self._market = market_service
        self._xtdata = xtdata_backend

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="xtquant_market_data",
            status="READY" if self._market is not None else "UNAVAILABLE",
            details={"delegate_injected": self._market is not None, "raw_backend_injected": self._xtdata is not None},
        )

    def get_bars(self, request: BarsRequest) -> Any:
        if request.start or request.end:
            if self._xtdata is None:
                raise ProviderCapabilityError("EXPLICIT_DATE_RANGE_REQUIRES_XTDATA_BACKEND")
            return self._xtdata.get_market_data_ex(
                field_list=[],
                stock_list=[request.symbol],
                period="1d",
                start_time=request.start,
                end_time=request.end,
                count=-1,
                dividend_type="none",
                fill_data=True,
            )
        return self._market.history(request.symbol, bars=request.bars, refresh=request.refresh)

    def get_snapshot(self, symbols: Sequence[str]) -> Mapping[str, Any]:
        return {symbol: self._market.full_tick(symbol) for symbol in symbols}

    def get_instrument(self, symbol: str, as_of: str | None = None) -> Any:
        if as_of not in (None, ""):
            raise ProviderCapabilityError("AS_OF_INSTRUMENT_DETAIL_NOT_SUPPORTED_BY_LEGACY_SERVICE")
        return self._market.instrument_detail(symbol)

    def _require_backend(self) -> Any:
        if self._xtdata is None:
            raise ProviderCapabilityError("XTDATA_BACKEND_REQUIRED")
        return self._xtdata

    def get_trading_days(self, start: str, end: str) -> Any:
        return self._require_backend().get_trading_dates("SH", start, end)

    def get_universe_members(self, universe: str, as_of: str | None = None) -> Any:
        if as_of not in (None, ""):
            raise ProviderCapabilityError("PIT_UNIVERSE_AS_OF_NOT_YET_IMPLEMENTED_IN_M1B1")
        return self._require_backend().get_stock_list_in_sector(universe)

    def get_index_weights(self, index: str, as_of: str | None = None) -> Any:
        backend = self._require_backend()
        if as_of not in (None, ""):
            raise ProviderCapabilityError("PIT_INDEX_WEIGHT_AS_OF_NOT_YET_IMPLEMENTED_IN_M1B1")
        return backend.get_index_weight(index)

    def get_corporate_actions(self, symbols: Sequence[str], start: str, end: str) -> Any:
        raise ProviderCapabilityError("CORPORATE_ACTIONS_NOT_YET_BOUND_IN_M1B1")

    def get_financials(self, request: Any) -> Any:
        raise ProviderCapabilityError("FINANCIALS_NOT_YET_BOUND_IN_M1B1")
