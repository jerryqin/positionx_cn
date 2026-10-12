from __future__ import annotations

from typing import Any

from .models import ApprovalContext, CancelRequest, OrderRequest, ProviderHealth


class XtQuantExecutionProvider:
    """Execution compatibility adapter around current MiniQmtBroker.

    Submit/cancel are fail-closed at this boundary: explicit ApprovalContext is mandatory.
    """

    def __init__(self, broker: Any):
        self._broker = broker

    def health(self) -> ProviderHealth:
        connected = bool(getattr(self._broker, "connected", False))
        return ProviderHealth(
            provider="xtquant_execution",
            status="READY" if connected else "UNKNOWN",
            details={"legacy_broker_injected": self._broker is not None, "connected_attr": connected},
        )

    def preview_order(self, request: OrderRequest) -> dict[str, Any]:
        return {
            "symbol": request.symbol,
            "side": request.side,
            "volume": int(request.volume),
            "price": float(request.price),
            "remark": request.remark,
            "submitted": False,
        }

    def submit_order(self, request: OrderRequest, approval_context: ApprovalContext) -> Any:
        approval_context.require()
        return self._broker.submit_limit(
            request.symbol,
            request.side,
            int(request.volume),
            float(request.price),
            request.remark,
        )

    def cancel_order(self, request: CancelRequest, approval_context: ApprovalContext) -> Any:
        approval_context.require()
        return self._broker.cancel(request.order_id)
