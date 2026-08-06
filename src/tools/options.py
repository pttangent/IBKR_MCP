"""Options trading tools for IBKR TWS API."""

from typing import Dict, Any, List, Optional
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from ib_async import Option, Stock
from ..models import AppContext


def register_options_tools(mcp: FastMCP):
    """Register options trading tools."""
    
    @mcp.tool()
    async def ibkr_calculate_option_price(
        ctx: Context[ServerSession, AppContext],
        symbol: str,
        expiration: str,
        strike: float,
        right: str,
        underlyingPrice: float,
        volatility: Optional[float] = None,
        exchange: str = "SMART",
        currency: str = "USD"
    ) -> Dict[str, Any]:
        """Calculate option price and greeks.
        
        Args:
            symbol: Underlying symbol
            expiration: Option expiration (YYYYMMDD format)
            strike: Strike price
            right: Call ('C') or Put ('P')
            underlyingPrice: Current underlying price for calculation
            volatility: Implied volatility (optional, will be calculated if not provided)
            exchange: Exchange (default: SMART)
            currency: Currency (default: USD)
            
        Returns:
            Option price, implied volatility, and greeks
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        option = Option(symbol, expiration, strike, right, exchange, currency=currency)
        
        # Qualify contract
        await tws.ib.qualifyContractsAsync(option)
        
        # Calculate option price
        if volatility:
            # Calculate option price from volatility
            calc = await tws.ib.calculateOptionPriceAsync(
                option,
                volatility=volatility,
                underPrice=underlyingPrice
            )
            
            return {
                "contract": {
                    "symbol": symbol,
                    "expiration": expiration,
                    "strike": strike,
                    "right": right
                },
                "optionPrice": calc.optPrice,
                "impliedVolatility": volatility,
                "delta": calc.delta,
                "gamma": calc.gamma,
                "vega": calc.vega,
                "theta": calc.theta,
                "underlyingPrice": underlyingPrice
            }
        else:
            # Get market price and calculate IV
            ticker = tws.ib.reqMktData(option)
            await tws.ib.sleep(2)  # Wait for market data
            
            if ticker.last and ticker.last > 0:
                optionPrice = ticker.last
            elif ticker.close and ticker.close > 0:
                optionPrice = ticker.close
            else:
                return {"error": "Unable to get option price"}
            
            tws.ib.cancelMktData(option)
            
            # Calculate IV from option price
            calc = await tws.ib.calculateImpliedVolatilityAsync(
                option,
                optionPrice=optionPrice,
                underPrice=underlyingPrice
            )
            
            return {
                "contract": {
                    "symbol": symbol,
                    "expiration": expiration,
                    "strike": strike,
                    "right": right
                },
                "optionPrice": optionPrice,
                "impliedVolatility": calc.impliedVolatility,
                "delta": calc.delta,
                "gamma": calc.gamma,
                "vega": calc.vega,
                "theta": calc.theta,
                "underlyingPrice": underlyingPrice
            }
    
    @mcp.tool()
    async def ibkr_get_option_chain(
        ctx: Context[ServerSession, AppContext],
        symbol: str,
        expiration: str,
        num_strikes: int = 10,
        exchange: str = "SMART",
        currency: str = "USD"
    ) -> Dict[str, Any]:
        """Get option chain for a specific expiration.
        
        Args:
            symbol: Underlying symbol
            expiration: Option expiration (YYYYMMDD format)
            num_strikes: Strikes around ATM (20=quick query, 50=deep research)
            exchange: Exchange (default: SMART)
            currency: Currency (default: USD)
            
        Returns:
            Option chain with calls, puts, and Greeks
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        # Get underlying contract
        stock = Stock(symbol, exchange, currency)
        await tws.ib.qualifyContractsAsync(stock)
        
        # Get option chain parameters
        chains = await tws.ib.reqSecDefOptParamsAsync(
            underlyingSymbol=symbol,
            futFopExchange="",
            underlyingSecType="STK",
            underlyingConId=stock.conId
        )
        
        if not chains:
            return {"error": f"No option chains found for {symbol}"}
        
        # Find best chain for this expiration (pick the one with most strikes)
        best_strikes = []
        best_trading_class = ""
        for chain in chains:
            if expiration in chain.expirations:
                s = sorted(chain.strikes)
                if len(s) > len(best_strikes):
                    best_strikes = s
                    best_trading_class = getattr(chain, 'tradingClass', '')
        
        if not best_strikes:
            return {"error": f"Expiration {expiration} not found. Available: {[e for c in chains for e in c.expirations[:5]]}"}
        
        strikes = best_strikes
        
        # Get underlying close price from historical bars (works 24/7)
        ref_price = strikes[len(strikes) // 2]  # fallback
        try:
            stock_bars = await tws.ib.reqHistoricalDataAsync(
                stock, endDateTime="", durationStr="1 D",
                barSizeSetting="1 day", whatToShow="TRADES",
                useRTH=True, formatDate=1)
            if stock_bars and stock_bars[-1].close > 0:
                ref_price = stock_bars[-1].close
        except Exception:
            pass
        
        mid = min(range(len(strikes)), key=lambda i: abs(strikes[i]-ref_price))
        half = num_strikes // 2
        strikes = strikes[max(0, mid-half):mid+half]
        
        # Filter to only strikes that actually exist in this chain
        strikes = [s for s in strikes if s in best_strikes]
        
        # Build contracts with the correct trading class
        calls = []
        puts = []
        tc = best_trading_class
        for strike in strikes:
            calls.append(Option(symbol, expiration, strike, 'C', exchange, currency=currency, tradingClass=tc))
            puts.append(Option(symbol, expiration, strike, 'P', exchange, currency=currency, tradingClass=tc))
        
        # Qualify each call/put pair individually
        chain_data = []
        for i, strike in enumerate(strikes):
            call = calls[i]
            put = puts[i]
            try:
                q = await tws.ib.qualifyContractsAsync(call, put)
            except Exception:
                continue
            # Filter out None (ambiguous/unresolved contracts)
            q_call = q[0] if len(q) > 0 and q[0] else None
            q_put = q[1] if len(q) > 1 and q[1] else None
            
            for j, opt_q in enumerate([q_call, q_put]):
                if opt_q is None:
                    continue
                try:
                    bars = await tws.ib.reqHistoricalDataAsync(
                        opt_q, endDateTime="", durationStr="1 D",
                        barSizeSetting="5 mins", whatToShow="TRADES",
                        useRTH=True, formatDate=1)
                    if bars and len(bars) > 0:
                        last = bars[-1]
                        entry = {
                            "strike": strike,
                            "right": "C" if j == 0 else "P",
                            "last": last.close,
                            "high": max(b.high for b in bars[-12:]),
                            "low": min(b.low for b in bars[-12:]),
                            "volume": sum(b.volume for b in bars),
                            "bars": len(bars),
                        }
                        chain_data.append(entry)
                except Exception:
                    continue
        
        return {
            "symbol": symbol,
            "expiration": expiration,
            "chain": chain_data,
            "strikeCount": len(strikes),
            "_v": "2.0",
            "_ref_price": ref_price
        }
    
    @mcp.tool()
    async def ibkr_place_option_order(
        ctx: Context[ServerSession, AppContext],
        symbol: str,
        expiration: str,
        strike: float,
        right: str,
        action: str,
        quantity: int,
        orderType: str = "MKT",
        limitPrice: Optional[float] = None,
        exchange: str = "SMART",
        currency: str = "USD"
    ) -> Dict[str, Any]:
        """Place an option order.
        
        Args:
            symbol: Underlying symbol
            expiration: Option expiration (YYYYMMDD format)
            strike: Strike price
            right: Call ('C') or Put ('P')
            action: BUY or SELL
            quantity: Number of contracts
            orderType: MKT, LMT, etc. (default: MKT)
            limitPrice: Limit price (required for LMT orders)
            exchange: Exchange (default: SMART)
            currency: Currency (default: USD)
            
        Returns:
            Order confirmation with orderId
        """
        tws = ctx.request_context.lifespan_context.tws
        if not tws or not tws.is_connected():
            return {"error": "TWS client not connected"}
        
        from ib_async import MarketOrder, LimitOrder
        
        option = Option(symbol, expiration, strike, right, exchange, currency=currency)
        
        # Qualify contract
        await tws.ib.qualifyContractsAsync(option)
        
        if orderType == "MKT":
            order = MarketOrder(action, quantity)
        elif orderType == "LMT":
            if limitPrice is None:
                return {"error": "limitPrice required for LMT orders"}
            order = LimitOrder(action, quantity, limitPrice)
        else:
            return {"error": f"Unsupported order type: {orderType}"}
        
        trade = tws.ib.placeOrder(option, order)
        
        return {
            "orderId": trade.order.orderId,
            "contract": {
                "symbol": symbol,
                "expiration": expiration,
                "strike": strike,
                "right": right
            },
            "action": action,
            "quantity": quantity,
            "orderType": orderType,
            "status": trade.orderStatus.status if trade.orderStatus else "Submitted"
        }
