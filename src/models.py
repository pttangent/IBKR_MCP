from pydantic import BaseModel
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .tws_client import TWSClient
    from .operational import RuntimeState


@dataclass
class AppContext:
    """Application context for MCP server."""
    tws: 'TWSClient'
    runtime: 'RuntimeState'


class ContractRequest(BaseModel):
    symbol: str
    secType: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"


class HistoricalDataRequest(BaseModel):
    contract: ContractRequest
    durationStr: str = "1 Y"
    barSizeSetting: str = "1 day"
    whatToShow: str = "TRADES"
    useRTH: int = 1


class OrderRequest(BaseModel):
    contract: ContractRequest
    action: str  # BUY or SELL
    totalQuantity: int
    orderType: str = "MKT"
    lmtPrice: Optional[float] = None
    auxPrice: Optional[float] = None
    transmit: bool = True


class PositionModel(BaseModel):
    account: str
    contract: Dict[str, Any]
    position: float
    avgCost: float


class AccountSummaryModel(BaseModel):
    tag: str
    value: str
    currency: str
    account: str


class OrderStatusModel(BaseModel):
    orderId: int
    status: str
    filled: float
    remaining: float
    avgFillPrice: float


class ExecutionModel(BaseModel):
    execId: str
    time: str
    acctNumber: str
    exchange: str
    side: str
    shares: float
    price: float
    permId: int
    clientId: int
    orderId: int
    liquidation: int
    cumQty: float
    avgPrice: float
    orderRef: Optional[str]
    evRule: Optional[str]
    evMultiplier: Optional[float]
    modelCode: Optional[str]
    lastLiquidity: Optional[int]
