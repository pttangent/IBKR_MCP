from ib_async import IB, Stock, Option, Future, Contract, MarketOrder, LimitOrder, util
from typing import List, Dict, Any, Optional, AsyncGenerator
import asyncio
from src.models import ContractRequest, OrderRequest


def _to_dict(obj):
    """Safely convert dataclass-like objects to dicts for tests and runtime.

    util.dataclassAsDict raises TypeError for MagicMocks used in unit tests.
    This helper falls back to __dict__ or attribute extraction when needed.
    """
    try:
        return util.dataclassAsDict(obj)
    except TypeError:
        # fall back to __dict__ if available
        if hasattr(obj, '__dict__'):
            return dict(obj.__dict__)
        # try to extract common fields as a last resort
        result = {}
        for attr in ('date', 'time', 'open', 'high', 'low', 'close', 'volume', 'symbol', 'conId', 'last'):
            if hasattr(obj, attr):
                value = getattr(obj, attr)
                # if value is a MagicMock with isoformat, call it
                try:
                    if hasattr(value, 'isoformat'):
                        value = value.isoformat()
                except Exception:
                    pass
                result[attr] = value
        if result:
            return result
        # give up - return a repr to avoid breaking callers
        return {"repr": repr(obj)}

class TWSClient:
    def __init__(self):
        # Do NOT create IB instance here to avoid capturing the wrong event loop.
        # The IB instance will be created in connect() in the correct event loop context.
        # We set it to None initially, and unit tests will need to set it up in their fixtures.
        self.ib: Optional[IB] = None
        self._connected = False
        self._market_data_subscriptions = {}

    def is_connected(self) -> bool:
        """Check if the client is connected to TWS."""
        return bool(self.ib and self.ib.isConnected())

    async def connect(self, host: str, port: int, client_id: int) -> bool:
        """Connect to TWS/IB Gateway."""
        if self.is_connected():
            return True

        # CRITICAL: Disconnect and completely destroy any existing IB instance
        # to prevent event loop capture issues
        if self.ib is not None:
            try:
                # Forcefully disconnect
                if hasattr(self.ib, 'disconnect'):
                    self.ib.disconnect()
                # Wait a moment for cleanup
                await asyncio.sleep(0.1)
            except Exception:
                pass
            finally:
                # Completely remove the reference
                del self.ib
                self.ib = None

        # Create a completely fresh IB instance in the current event loop context
        # This is the ONLY way to ensure the IB instance is bound to the correct loop
        self.ib = IB()

        try:
            # Use a shorter timeout to avoid MCP session cancellation
            # ib_async connectAsync is a coroutine
            await asyncio.wait_for(
                self.ib.connectAsync(host, port, clientId=client_id, timeout=5),
                timeout=8.0  # Overall timeout slightly longer than connectAsync timeout
            )
            self._connected = True
            return True
        except asyncio.TimeoutError:
            # Cleanup on timeout
            try:
                if self.ib:
                    self.ib.disconnect()
            except Exception:
                pass
            raise ConnectionError(
                f"Connection timeout: Could not connect to TWS at {host}:{port} within 8 seconds. "
                f"Please ensure TWS/IB Gateway is running and accepting connections on this port."
            )
        except asyncio.CancelledError:
            # Request was cancelled by the client
            try:
                if self.ib:
                    self.ib.disconnect()
            except Exception:
                pass
            raise ConnectionError(
                f"Connection cancelled: The connection request to {host}:{port} was cancelled. "
                f"This may indicate a client timeout or disconnection."
            )
        except Exception as e:
            # Other connection errors
            try:
                if self.ib and self.ib.isConnected():
                    self.ib.disconnect()
            except Exception:
                pass
            raise ConnectionError(
                f"Failed to connect to TWS at {host}:{port}: {type(e).__name__}: {str(e)}"
            )

    def disconnect(self):
        """Disconnect from TWS/IB Gateway."""
        if self.ib and self.ib.isConnected():
            try:
                self.ib.disconnect()
            except Exception:
                pass
        self._connected = False

    def _create_contract(self, req: ContractRequest) -> Contract:
        """Helper to create an ib_insync Contract object."""
        if req.secType == "STK":
            return Stock(req.symbol, req.exchange, req.currency)
        elif req.secType == "OPT":
            # Simplified for now, full implementation would require more fields
            return Option(req.symbol, exchange=req.exchange, currency=req.currency)
        elif req.secType == "FUT":
            # Simplified for now
            return Future(req.symbol, exchange=req.exchange, currency=req.currency)
        else:
            return Contract(symbol=req.symbol, secType=req.secType, exchange=req.exchange, currency=req.currency)

    async def get_contract_details(self, req: ContractRequest) -> List[Dict[str, Any]]:
        """Get contract details for a given contract request."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        contract = self._create_contract(req)
        
        # Set up error handler to capture IB errors
        error_occurred = []
        
        def on_error(reqId, errorCode, errorString, contract):
            # Filter out warnings - these are informational only
            WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
            is_warning = errorCode in WARNING_CODES or 2100 <= errorCode < 2200
            if not is_warning:
                error_occurred.append({
                    'reqId': reqId,
                    'errorCode': errorCode,
                    'errorString': errorString,
                    'contract': str(contract) if contract else 'N/A'
                })
        
        # Connect to error event
        self.ib.errorEvent += on_error
        
        try:
            details = await self.ib.reqContractDetailsAsync(contract)
            
            # Check if any errors occurred during the request
            if error_occurred:
                error = error_occurred[0]
                raise RuntimeError(
                    f"TWS Error {error['errorCode']}: {error['errorString']} "
                    f"(reqId: {error['reqId']}, contract: {error['contract']})"
                )
            
            return [_to_dict(cd) for cd in details]
        finally:
            # Always disconnect the error handler
            try:
                self.ib.errorEvent -= on_error
            except Exception:
                pass

    async def search_symbols(self, pattern: str) -> List[Dict[str, Any]]:
        """Search for contracts by partial symbol match using reqMatchingSymbols.
        
        Args:
            pattern: Partial symbol or company name to search for
            
        Returns:
            List of matching contract descriptions
        """
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        # Set up error handler to capture IB errors
        error_occurred = []
        
        def on_error(reqId, errorCode, errorString, contract):
            # Filter out warnings - these are informational only
            WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
            is_warning = errorCode in WARNING_CODES or 2100 <= errorCode < 2200
            if not is_warning:
                error_occurred.append({
                    'reqId': reqId,
                    'errorCode': errorCode,
                    'errorString': errorString,
                    'contract': str(contract) if contract else 'N/A'
                })
        
        # Connect to error event
        self.ib.errorEvent += on_error
        
        try:
            # Use reqMatchingSymbols to search for contracts by pattern
            contract_descriptions = await self.ib.reqMatchingSymbolsAsync(pattern)
            
            # Check if any errors occurred during the request
            if error_occurred:
                error = error_occurred[0]
                raise RuntimeError(
                    f"TWS Error {error['errorCode']}: {error['errorString']} "
                    f"(reqId: {error['reqId']}, contract: {error['contract']})"
                )
            
            # Convert ContractDescription objects to dicts
            return [_to_dict(cd) for cd in contract_descriptions]
        finally:
            # Always disconnect the error handler
            try:
                self.ib.errorEvent -= on_error
            except Exception:
                pass

    async def get_historical_data(self, req: ContractRequest, durationStr: str, barSizeSetting: str, whatToShow: str) -> List[Dict[str, Any]]:
        """Get historical market data."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")

        contract = self._create_contract(req)
        
        # Validate that contract was created successfully
        if contract is None:
            raise ValueError(f"Failed to create contract for {req.symbol} (secType={req.secType})")
        
        # Qualify the contract first
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Contract not found or could not be qualified: {req.symbol} (secType={req.secType}, exchange={req.exchange})")
        
        # Ensure the qualified contract is valid
        qualified_contract = qualified[0]
        if qualified_contract is None:
            raise ValueError(f"Contract qualification returned None for {req.symbol} (secType={req.secType}, exchange={req.exchange})")

        # Set up error handler to capture IB errors
        error_occurred = []
        
        def on_error(reqId, errorCode, errorString, contract):
            # Filter out warnings - these are informational only
            WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
            is_warning = errorCode in WARNING_CODES or 2100 <= errorCode < 2200
            if not is_warning:
                error_occurred.append({
                    'reqId': reqId,
                    'errorCode': errorCode,
                    'errorString': errorString,
                    'contract': str(contract) if contract else 'N/A'
                })
        
        # Connect to error event
        self.ib.errorEvent += on_error
        
        try:
            bars = await self.ib.reqHistoricalDataAsync(
                qualified_contract,
                endDateTime='',
                durationStr=durationStr,
                barSizeSetting=barSizeSetting,
                whatToShow=whatToShow,
                useRTH=1,
                formatDate=1
            )
            
            # Check if any errors occurred during the request
            if error_occurred:
                error = error_occurred[0]
                raise RuntimeError(
                    f"TWS Error {error['errorCode']}: {error['errorString']} "
                    f"(reqId: {error['reqId']}, contract: {error['contract']})"
                )
            
            return [_to_dict(bar) for bar in bars]
        finally:
            # Always disconnect the error handler
            try:
                self.ib.errorEvent -= on_error
            except Exception:
                pass

    async def get_account_summary(self) -> List[Dict[str, Any]]:
        """Get account summary values."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        summary = await self.ib.accountSummaryAsync()
        return [_to_dict(item) for item in summary]

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current portfolio positions."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        positions = self.ib.positions()
        return [
            {
                "account": pos.account,
                "contract": _to_dict(pos.contract),
                "position": pos.position,
                "avgCost": pos.avgCost,
            }
            for pos in positions
        ]

    async def place_order(self, req: OrderRequest) -> Dict[str, Any]:
        """Place an order."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")

        contract = self._create_contract(req.contract)
        
        # Qualify the contract
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Contract not found or could not be qualified: {req.contract.symbol}")
        contract = qualified[0]

        # Create order
        if req.orderType == "MKT":
            order = MarketOrder(req.action, req.totalQuantity, transmit=req.transmit)
        elif req.orderType == "LMT":
            if req.lmtPrice is None:
                raise ValueError("limitPrice is required for LMT orders")
            order = LimitOrder(req.action, req.totalQuantity, req.lmtPrice, transmit=req.transmit)
        else:
            raise ValueError(f"Unsupported order type: {req.orderType}")
        
        # Place order
        trade = self.ib.placeOrder(contract, order)
        
        # Wait a moment for status update
        await asyncio.sleep(0.5) 
        
        # Return trade details
        return {
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status,
            "contract": _to_dict(trade.contract),
            "action": trade.order.action,
            "quantity": trade.order.totalQuantity,
        }

    async def cancel_order(self, orderId: int) -> Dict[str, Any]:
        """Cancel an order by ID."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        order = await self.ib.reqOpenOrdersAsync()
        order_to_cancel = next((o for o in order if o.order.orderId == orderId), None)
        
        if not order_to_cancel:
            raise ValueError(f"Order with ID {orderId} not found among open orders.")

        trade = self.ib.cancelOrder(order_to_cancel.order)
        
        # Wait a moment for status update
        await asyncio.sleep(0.5)

        return {
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status,
            "message": f"Cancellation request sent for order {orderId}"
        }

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        trades = await self.ib.reqAllOpenOrdersAsync()
        return [
            {
                "orderId": t.order.orderId,
                "status": t.orderStatus.status,
                "contract": _to_dict(t.contract),
                "action": t.order.action,
                "quantity": t.order.totalQuantity,
            }
            for t in trades
        ]

    async def get_executions(self) -> List[Dict[str, Any]]:
        """Get all executions."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        executions = await self.ib.reqExecutionsAsync()
        return [_to_dict(e) for e in executions]

    async def get_pnl(self, account: str, modelCode: str) -> Dict[str, Any]:
        """Get overall Profit and Loss."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        # reqPnL starts a subscription and returns a PnL object
        # The object is kept live-updated by TWS
        pnl_obj = self.ib.reqPnL(account, modelCode)
        
        # Wait for the IB.pnlEvent to fire with updates (up to 5 seconds)
        # The pnlEvent fires when any PnL subscription receives data
        received_update = False
        
        def on_pnl_update(pnl):
            nonlocal received_update
            # Check if this update is for our subscription
            if pnl.account == account and pnl.modelCode == modelCode:
                received_update = True
        
        # Subscribe to pnl events
        self.ib.pnlEvent += on_pnl_update
        
        try:
            # Wait for update or timeout
            timeout_time = asyncio.get_event_loop().time() + 5.0
            while not received_update and asyncio.get_event_loop().time() < timeout_time:
                await asyncio.sleep(0.1)
            
            if not received_update:
                raise asyncio.TimeoutError()
                
        except asyncio.TimeoutError:
            # Cancel the subscription and raise error
            self.ib.cancelPnL(account, modelCode)
            self.ib.pnlEvent -= on_pnl_update
            raise RuntimeError(
                f"Timeout waiting for P&L data for account {account}. "
                f"Ensure the account is valid and has positions."
            )
        finally:
            # Always remove the event handler
            self.ib.pnlEvent -= on_pnl_update
        
        # Cancel the subscription before returning (we only need a snapshot)
        self.ib.cancelPnL(account, modelCode)
        
        return _to_dict(pnl_obj)

    async def get_pnl_single(self, account: str, modelCode: str, conId: int) -> Dict[str, Any]:
        """Get PnL for a single account/model."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        # reqPnLSingle starts a subscription and returns a PnLSingle object
        # The object is kept live-updated by TWS
        pnl_obj = self.ib.reqPnLSingle(account, modelCode, conId)
        
        # Wait for the IB.pnlSingleEvent to fire with updates (up to 5 seconds)
        # The pnlSingleEvent fires when any PnLSingle subscription receives data
        received_update = False
        
        def on_pnl_update(pnl):
            nonlocal received_update
            # Check if this update is for our subscription
            if pnl.account == account and pnl.conId == conId and pnl.modelCode == modelCode:
                received_update = True
        
        # Subscribe to pnlSingle events
        self.ib.pnlSingleEvent += on_pnl_update
        
        try:
            # Wait for update or timeout
            timeout_time = asyncio.get_event_loop().time() + 5.0
            while not received_update and asyncio.get_event_loop().time() < timeout_time:
                await asyncio.sleep(0.1)
            
            if not received_update:
                raise asyncio.TimeoutError()
                
        except asyncio.TimeoutError:
            # Cancel the subscription and raise error
            self.ib.cancelPnLSingle(account, modelCode, conId)
            self.ib.pnlSingleEvent -= on_pnl_update
            raise RuntimeError(
                f"Timeout waiting for P&L data for account {account}, conId {conId}. "
                f"Ensure the account is valid and has a position for this contract."
            )
        finally:
            # Always remove the event handler
            self.ib.pnlSingleEvent -= on_pnl_update
        
        # Cancel the subscription before returning (we only need a snapshot)
        self.ib.cancelPnLSingle(account, modelCode, conId)
        
        return _to_dict(pnl_obj)

    async def stream_market_data(self, req: ContractRequest) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream real-time market data."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")

        contract = self._create_contract(req)
        
        # Qualify the contract first
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Contract not found or could not be qualified: {req.symbol}")
        contract = qualified[0]

        # Use the contract's conId as the key
        con_id = contract.conId
        if con_id in self._market_data_subscriptions:
            raise RuntimeError(f"Market data already streaming for contract ID {con_id}")

        # Request market data
        self.ib.reqMarketDataType(3)  # 3 = delayed data
        ticker = self.ib.reqMktData(contract, '', False, False)
        self._market_data_subscriptions[con_id] = ticker
        
        # Set up error tracking BEFORE waiting for initial data
        # Warning codes from ib_async/wrapper.py - these are informational and should not stop streaming
        # 10167: "Requested market data is not subscribed. Displaying delayed market data."
        # Other warning codes: 105, 110, 165, 321, 329, 399, 404, 434, 492, and 2100-2199 range
        WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
        
        error_occurred = []
        
        def on_error(reqId, errorCode, errorString, contract):
            """Callback for TWS errors related to this request"""
            # Check if this error is for our ticker
            if contract and hasattr(contract, 'conId') and contract.conId == con_id:
                # Filter out warnings - these are informational only
                is_warning = errorCode in WARNING_CODES or 2100 <= errorCode < 2200
                if not is_warning:
                    error_occurred.append({
                        'reqId': reqId,
                        'errorCode': errorCode,
                        'errorString': errorString,
                        'contract': str(contract)
                    })
        
        # Connect to error event
        self.ib.errorEvent += on_error

        try:
            # Wait for initial snapshot - give TWS time to send initial data
            print(f"[STREAM DEBUG] Requested market data for {contract.symbol}, waiting for initial snapshot...")
            await asyncio.sleep(1.0)  # Wait for initial data
            print(f"[STREAM DEBUG] Got initial snapshot for {contract.symbol}: time={ticker.time}, last={ticker.last}, bid={ticker.bid}, ask={ticker.ask}")
            
            # Check for immediate errors (like missing market data subscription)
            await asyncio.sleep(0.5)  # Give TWS time to send error if any
            
            if error_occurred:
                error = error_occurred[0]
                raise RuntimeError(
                    f"TWS Error {error['errorCode']}: {error['errorString']} "
                    f"(reqId: {error['reqId']}, contract: {error['contract']})"
                )
            
            # Yield initial snapshot if available
            has_price = ticker.last or ticker.bid or ticker.ask
            if ticker.time and has_price:
                print(f"[STREAM DEBUG] Yielding initial snapshot for {contract.symbol}")
                yield {
                    "time": ticker.time.isoformat(),
                    "last": ticker.last,
                    "bid": ticker.bid,
                    "ask": ticker.ask,
                    "volume": ticker.volume,
                    "bidSize": ticker.bidSize,
                    "askSize": ticker.askSize,
                    "close": ticker.close,
                }
            
            # Track the last timestamp we yielded to avoid duplicates
            last_time_yielded = ticker.time if (ticker.time and has_price) else None
            
            while True:
                # Check for errors that occurred during streaming
                if error_occurred:
                    error = error_occurred[0]
                    raise RuntimeError(
                        f"TWS Error {error['errorCode']}: {error['errorString']} "
                        f"(reqId: {error['reqId']}, contract: {error['contract']})"
                    )
                
                # Wait for IB to process updates (this lets the event loop run)
                await self.ib.updateEvent
                
                # Check if ticker has new data
                # For stocks: ticker.last is the primary field
                # For forex (CASH): bid/ask are the primary fields
                has_price = ticker.last or ticker.bid or ticker.ask
                
                if ticker.time and has_price:
                    # Only yield if this is new data (different timestamp)
                    if last_time_yielded is None or ticker.time != last_time_yielded:
                        print(f"[STREAM DEBUG] {contract.symbol} - New data: time={ticker.time}, last={ticker.last}, bid={ticker.bid}, ask={ticker.ask}")
                        last_time_yielded = ticker.time
                        yield {
                            "time": ticker.time.isoformat(),
                            "last": ticker.last,
                            "bid": ticker.bid,
                            "ask": ticker.ask,
                            "volume": ticker.volume,
                            "bidSize": ticker.bidSize,
                            "askSize": ticker.askSize,
                            "close": ticker.close,
                        }
                    else:
                        # Same timestamp, no new data yet
                        print(f"[STREAM DEBUG] {contract.symbol} - Same timestamp, waiting for new data...")
                else:
                    # No meaningful update yet
                    print(f"[STREAM DEBUG] {contract.symbol} - No price data yet")
                    yield {}
        
        except asyncio.CancelledError:
            # Clean up when the generator is closed
            self.ib.errorEvent -= on_error  # Disconnect error handler
            self.ib.cancelMktData(contract)
            del self._market_data_subscriptions[con_id]
            raise
        
        except GeneratorExit:
            # Clean up when the generator is explicitly closed via aclose()
            self.ib.errorEvent -= on_error  # Disconnect error handler
            self.ib.cancelMktData(contract)
            del self._market_data_subscriptions[con_id]
            raise

        except Exception as e:
            # Clean up on other errors
            self.ib.errorEvent -= on_error  # Disconnect error handler
            if con_id in self._market_data_subscriptions:
                self.ib.cancelMktData(contract)
                del self._market_data_subscriptions[con_id]
            raise
        
        finally:
            # Always disconnect the error handler
            try:
                self.ib.errorEvent -= on_error
            except Exception:
                pass

    async def stream_account_updates(self, account: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream real-time account updates using event-driven approach.
        
        This uses IB's updatePortfolio and updateAccountValue events to get
        real-time notifications when positions or account values change.
        """
        print(f"[TWS CLIENT] stream_account_updates called for {account}")
        
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")

        # Use the lower-level client API directly to avoid event loop issues
        # This sends the request without waiting for a response
        print(f"[TWS CLIENT] Requesting account updates for {account}...")
        # Client.reqAccountUpdates(subscribe: bool, acctCode: str)
        self.ib.client.reqAccountUpdates(True, account)
        print(f"[TWS CLIENT] Account updates request sent for {account}")
        
        # Give TWS a moment to start sending data
        await asyncio.sleep(0.5)
        
        # Queue to collect events
        event_queue: asyncio.Queue = asyncio.Queue()
        print(f"[TWS CLIENT] Event queue created")
        
        # Event handler for portfolio updates
        def on_update_portfolio(item):
            """Called when a position is updated."""
            print(f"[TWS CLIENT] on_update_portfolio fired: {item.account} == {account}? {item.account == account}")
            if item.account == account:
                print(f"[TWS CLIENT] Adding portfolio update to queue: {item.contract.symbol}")
                event_queue.put_nowait({
                    "type": "portfolio_item",
                    "data": {
                        "account": item.account,
                        "contract": _to_dict(item.contract),
                        "position": item.position,
                        "marketPrice": item.marketPrice,
                        "marketValue": item.marketValue,
                        "averageCost": item.averageCost,
                        "unrealizedPNL": item.unrealizedPNL,
                        "realizedPNL": item.realizedPNL,
                    }
                })
        
        # Event handler for account value updates
        def on_update_account_value(item):
            """Called when an account value is updated."""
            print(f"[TWS CLIENT] on_update_account_value fired: {item.account} == {account}? {item.account == account}")
            if item.account == account:
                print(f"[TWS CLIENT] Adding account value to queue: {item.tag}")
                event_queue.put_nowait({
                    "type": "account_value",
                    "data": {
                        "account": item.account,
                        "tag": item.tag,
                        "value": item.value,
                        "currency": item.currency,
                        "modelCode": item.modelCode,
                    }
                })
        
        # Subscribe to events
        print(f"[TWS CLIENT] Subscribing to portfolio events for account {account}")
        self.ib.updatePortfolioEvent += on_update_portfolio
        self.ib.accountValueEvent += on_update_account_value
        print(f"[TWS CLIENT] Event handlers attached")

        try:
            # Give a moment for initial events to fire and populate queue
            await asyncio.sleep(0.5)
            
            # Yield any initial queued events
            while not event_queue.empty():
                event = event_queue.get_nowait()
                print(f"[TWS CLIENT] Yielding initial event from queue: {event['type']}")
                yield event
            
            # Enter main streaming loop
            print(f"[TWS CLIENT] Entering main streaming loop for {account}")
            while True:
                # Wait for IB to process updates (same as market data streaming)
                # This is the key: it allows the event loop to run and fire our event handlers
                await self.ib.updateEvent
                
                # Check if we have any queued events
                if not event_queue.empty():
                    event = event_queue.get_nowait()
                    print(f"[TWS CLIENT] Yielding event from queue: {event['type']}")
                    yield event
                else:
                    # No new data, yield empty dict to keep generator alive
                    yield {}

        except asyncio.CancelledError:
            # Clean up when the generator is closed
            self.ib.updatePortfolioEvent -= on_update_portfolio
            self.ib.accountValueEvent -= on_update_account_value
            self.ib.client.reqAccountUpdates(False, account)
            raise

        except Exception as e:
            # Clean up on other errors
            self.ib.updatePortfolioEvent -= on_update_portfolio
            self.ib.accountValueEvent -= on_update_account_value
            self.ib.client.reqAccountUpdates(False, account)
            raise
            # Clean up on other errors
            self.ib.updatePortfolioEvent -= on_update_portfolio
            self.ib.updateAccountValueEvent -= on_update_account_value
            self.ib.cancelAccountUpdates(account)
            raise

    async def get_news_providers(self) -> List[Dict[str, Any]]:
        """Get available news providers."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        providers = await self.ib.reqNewsProvidersAsync()
        
        # Handle None or empty response
        if providers is None:
            return []
        
        return [_to_dict(provider) for provider in providers]

    async def get_historical_news(
        self, 
        req: ContractRequest, 
        providerCodes: str, 
        startDateTime: str, 
        endDateTime: str, 
        totalResults: int
    ) -> List[Dict[str, Any]]:
        """Get historical news headlines for a contract."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")

        contract = self._create_contract(req)
        
        # Qualify the contract to get conId
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Contract not found or could not be qualified: {req.symbol}")
        
        qualified_contract = qualified[0]
        if not qualified_contract.conId:
            raise ValueError(f"Contract has no conId: {req.symbol}")

        # Set up error handler to capture IB errors
        error_occurred = []
        
        def on_error(reqId, errorCode, errorString, contract):
            # Filter out warnings
            WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
            is_warning = errorCode in WARNING_CODES or 2100 <= errorCode < 2200
            if not is_warning:
                error_occurred.append({
                    'reqId': reqId,
                    'errorCode': errorCode,
                    'errorString': errorString,
                    'contract': str(contract) if contract else 'N/A'
                })
        
        # Connect to error event
        self.ib.errorEvent += on_error
        
        try:
            # Request historical news
            news = await self.ib.reqHistoricalNewsAsync(
                conId=qualified_contract.conId,
                providerCodes=providerCodes,
                startDateTime=startDateTime,
                endDateTime=endDateTime,
                totalResults=totalResults
            )
            
            # Check if any errors occurred during the request
            if error_occurred:
                error = error_occurred[0]
                raise RuntimeError(
                    f"TWS Error {error['errorCode']}: {error['errorString']} "
                    f"(reqId: {error['reqId']}, contract: {error['contract']})"
                )
            
            # Handle None or empty response
            if news is None:
                return []
            
            return [_to_dict(article) for article in news]
        finally:
            # Always disconnect the error handler
            try:
                self.ib.errorEvent -= on_error
            except Exception:
                pass

    async def get_news_article(self, providerCode: str, articleId: str) -> Dict[str, Any]:
        """Get the full text of a news article."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        # Set up error handler to capture IB errors
        error_occurred = []
        
        def on_error(reqId, errorCode, errorString, contract):
            # Filter out warnings
            WARNING_CODES = frozenset({105, 110, 165, 321, 329, 399, 404, 434, 492, 10167})
            is_warning = errorCode in WARNING_CODES or 2100 <= errorCode < 2200
            if not is_warning:
                error_occurred.append({
                    'reqId': reqId,
                    'errorCode': errorCode,
                    'errorString': errorString,
                    'contract': str(contract) if contract else 'N/A'
                })
        
        # Connect to error event
        self.ib.errorEvent += on_error
        
        try:
            # Request news article
            article = await self.ib.reqNewsArticleAsync(
                providerCode=providerCode,
                articleId=articleId
            )
            
            # Check if any errors occurred during the request
            if error_occurred:
                error = error_occurred[0]
                raise RuntimeError(
                    f"TWS Error {error['errorCode']}: {error['errorString']} "
                    f"(reqId: {error['reqId']}, contract: {error['contract']})"
                )
            
            # Handle None response
            if article is None:
                raise RuntimeError(
                    f"No article found for providerCode={providerCode}, articleId={articleId}"
                )
            
            return _to_dict(article)
        finally:
            # Always disconnect the error handler
            try:
                self.ib.errorEvent -= on_error
            except Exception:
                pass

    async def subscribe_news_bulletins(self, allMessages: bool) -> Dict[str, Any]:
        """Subscribe to real-time IB news bulletins."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        
        # Subscribe to news bulletins
        self.ib.reqNewsBulletins(allMessages)
        
        return {
            "status": "subscribed",
            "allMessages": allMessages,
            "message": "News bulletins subscription started. Bulletins will be delivered via IB events."
        }

    async def qualify_contract(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART", currency: str = "USD"):
        """Helper to qualify a contract from symbol."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        from ib_async import Stock
        contract = Stock(symbol, exchange, currency)
        qualified = await self.ib.qualifyContractsAsync(contract)
        return qualified[0]

    async def get_fundamental_data(self, contract, report_type: str):
        """Get Reuters fundamental data for a contract."""
        if not self.is_connected():
            raise RuntimeError("Not connected to TWS")
        try:
            data = await self.ib.reqFundamentalDataAsync(contract, report_type)
            return {"xml": str(data)[:50000]} if data else {"empty": True}
        except Exception as e:
            raise RuntimeError(f"Fundamental data not available: {e}")
