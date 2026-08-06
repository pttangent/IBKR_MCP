"""Advanced trading tools for IBKR TWS API."""

from typing import Dict, Any, List, Optional
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from ib_async import Stock, Contract
from ..models import AppContext


def register_advanced_tools(mcp: FastMCP):
    """Register advanced trading tools."""
    
    @mcp.tool()
    async def ibkr_get_matching_symbols(
        ctx: Context[ServerSession, AppContext],
        pattern: str
    ) -> Dict[str, Any]:
        """Search for symbols using smart matching.
        
        Args:
            pattern: Search pattern (can be partial symbol or company name)
            
        Returns:
            List of matching contracts with descriptions
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        matches = await tws.ib.reqMatchingSymbolsAsync(pattern)
        
        results = []
        for desc in matches:
            c = desc.contract
            results.append({
                "conId": c.conId,
                "symbol": c.symbol,
                "secType": c.secType,
                "primaryExchange": c.primaryExchange,
                "currency": c.currency,
                "derivativeSecTypes": desc.derivativeSecTypes
            })
        
        return {"results": results, "count": len(results)}
    
    @mcp.tool()
    async def ibkr_get_tick_by_tick_data(
        ctx: Context[ServerSession, AppContext],
        symbol: str,
        tickType: str = "Last",
        numberOfTicks: int = 100,
        startDateTime: str = "",
        exchange: str = "SMART",
        currency: str = "USD"
    ) -> Dict[str, Any]:
        """Get historical tick-by-tick data.
        
        Args:
            symbol: Contract symbol
            tickType: Last, BidAsk, MidPoint, or AllLast (default: Last)
            numberOfTicks: Number of ticks to retrieve (max: 1000)
            startDateTime: Start time in IB format 'YYYYMMDD HH:mm:ss US/Eastern' (default: today 9:30 ET)
            exchange: Exchange (default: SMART)
            currency: Currency (default: USD)
            
        Returns:
            Tick-by-tick historical data
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        from datetime import datetime
        if not startDateTime:
            today = datetime.now().strftime("%Y%m%d")
            startDateTime = f"{today} 09:30:00 US/Eastern"
        
        contract = Stock(symbol, exchange, currency)
        qualified = await tws.ib.qualifyContractsAsync(contract)
        
        ticks = await tws.ib.reqHistoricalTicksAsync(
            qualified[0],
            startDateTime=startDateTime,
            endDateTime='',
            numberOfTicks=numberOfTicks,
            whatToShow='TRADES' if tickType == 'Last' else tickType,
            useRth=True,
            ignoreSize=False
        )
        
        results = []
        for tick in ticks:
            tick_data = {
                "time": tick.time.isoformat() if hasattr(tick.time, 'isoformat') else str(tick.time)
            }
            
            if tickType == "BidAsk":
                tick_data.update({
                    "priceBid": tick.priceBid,
                    "priceAsk": tick.priceAsk,
                    "sizeBid": tick.sizeBid,
                    "sizeAsk": tick.sizeAsk
                })
            else:
                tick_data.update({
                    "price": tick.price,
                    "size": tick.size
                })
            
            results.append(tick_data)
        
        return {
            "symbol": symbol,
            "tickType": tickType,
            "ticks": results,
            "count": len(results)
        }
    
    @mcp.tool()
    async def ibkr_get_smart_components(
        ctx: Context[ServerSession, AppContext],
        bboExchange: str
    ) -> Dict[str, Any]:
        """Get mapping of single letter codes to exchange names.
        
        Args:
            bboExchange: Exchange code from market data
            
        Returns:
            Map of exchange codes to names
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        components = await tws.ib.reqSmartComponentsAsync(bboExchange)
        
        return {
            "bboExchange": bboExchange,
            "components": {
                comp.bitNumber: comp.exchange
                for comp in components
            }
        }
    
    @mcp.tool()
    async def ibkr_get_security_definition_by_conid(
        ctx: Context[ServerSession, AppContext],
        conId: int
    ) -> Dict[str, Any]:
        """Get contract details by contract ID.
        
        Args:
            conId: Contract ID
            
        Returns:
            Full contract details
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        contract = Contract(conId=conId)
        details = await tws.ib.reqContractDetailsAsync(contract)
        
        if not details:
            return {"error": f"No contract found with conId {conId}"}
        
        detail = details[0]
        c = detail.contract
        
        return {
            "contract": {
                "conId": c.conId,
                "symbol": c.symbol,
                "secType": c.secType,
                "exchange": c.exchange,
                "primaryExchange": c.primaryExchange,
                "currency": c.currency,
                "localSymbol": c.localSymbol,
                "tradingClass": c.tradingClass,
                "multiplier": c.multiplier
            },
            "details": {
                "marketName": detail.marketName,
                "minTick": detail.minTick,
                "orderTypes": detail.orderTypes,
                "validExchanges": detail.validExchanges,
                "longName": detail.longName,
                "industry": detail.industry,
                "category": detail.category,
                "subcategory": detail.subcategory
            }
        }
    
    @mcp.tool()
    async def ibkr_get_wsh_meta_data(
        ctx: Context[ServerSession, AppContext]
    ) -> Dict[str, Any]:
        """Get Wall Street Horizon metadata for available event types.
        
        Returns:
            Available WSH event types and categories
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        metadata = await tws.ib.getWshMetaDataAsync()
        
        return {
            "metadata": metadata,
            "description": "Wall Street Horizon event types and filters"
        }
    
    @mcp.tool()
    async def ibkr_get_wsh_event_data(
        ctx: Context[ServerSession, AppContext],
        conId: int,
        startDate: str = "",
        endDate: str = "",
        totalLimit: int = 10
    ) -> Dict[str, Any]:
        """Get Wall Street Horizon event data for a contract.
        
        Args:
            conId: Contract ID
            startDate: Start date (YYYYMMDD format, empty for today)
            endDate: End date (YYYYMMDD format, empty for far future)
            totalLimit: Maximum events to retrieve
            
        Returns:
            Corporate events from Wall Street Horizon
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        from ib_async import WshEventData
        
        wsh_data = WshEventData()
        wsh_data.conId = conId
        wsh_data.startDate = startDate
        wsh_data.endDate = endDate
        wsh_data.totalLimit = totalLimit
        
        events = await tws.ib.getWshEventDataAsync(wsh_data)
        
        return {
            "conId": conId,
            "events": events,
            "count": len(events) if isinstance(events, list) else 1
        }
