from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


class ProviderError(RuntimeError):
    """Base provider-layer error."""


class ProviderCapabilityError(ProviderError):
    """Requested provider capability is not available from the configured backend."""


class ProviderApprovalError(ProviderError):
    """Execution action was requested without an explicit valid approval context."""


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    status: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BarsRequest:
    symbol: str
    bars: int = 260
    refresh: bool = False
    start: str = ""
    end: str = ""


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    volume: int
    price: float
    remark: str = ""


@dataclass(frozen=True)
class CancelRequest:
    order_id: Any


@dataclass(frozen=True)
class ApprovalContext:
    approved: bool
    token: str
    actor: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def require(self) -> None:
        if not self.approved or not str(self.token).strip():
            raise ProviderApprovalError("EXPLICIT_APPROVAL_REQUIRED")
