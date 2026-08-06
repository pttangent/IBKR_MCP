# Paper Account Data and TWS API Limits

Status date: **2026-08-06**. This repository uses the **TWS socket API** through `ib_async`; Client Portal Web API limits are not interchangeable with the limits below.

## Paper account without additional market-data subscriptions

A paper account can use TWS API account, position, P&L and order functions, but the paper environment does not create a separate free real-time data entitlement. Trading permissions, market-data subscriptions and other settings are inherited from, or shared with, the associated live account.

Without paid or shared subscriptions, expect the following:

| Capability | Expected result |
|---|---|
| Account balances, positions, open orders, executions, P&L | Available |
| Paper orders | Available, subject to product permissions and simulator limitations |
| US stock and option live quotes | Not available unless the paper username owns or shares the relevant subscriptions |
| Delayed stock watchlist data | Often available; usually 15–20 minutes delayed |
| Historical bars | Often available through the delayed-data path, depending on contract and exchange |
| Real-time bars, tick-by-tick, market depth | Not available through delayed data |
| Option Greeks | Usually unavailable or incomplete unless both the option and underlying permissions are present |
| Scanner rankings | Available without a market-data subscription, but scan results do not include prices |
| IBKR system bulletins | Available; these are operational messages, not company news |
| Free API news | BRFUPDN, BRFG and DJNL after enabling them in TWS/IB Gateway API News Configuration |

The server therefore exposes three operating profiles after `ibkr_probe_capabilities`:

- `LIVE_FULL`: a test symbol returned fresh live data. Intraday radar is permitted.
- `DELAYED_ONLY`: prices exist but are delayed/frozen. Research and low-frequency observation are permitted; order automation is blocked.
- `ACCOUNT_ONLY`: account tools may work but usable market data was not detected.

A successful probe for one symbol does **not** prove permission for every exchange. For example, SPY and AAPL may require different US network entitlements. Start an `ibkr://operational-market-data/{symbol}` stream for the exact order symbol before using the guarded order tool.

## Paper simulator limitations

IBKR documents material differences from live execution:

- Fills are simulated from the top of book; queue position and deep liquidity are not modeled.
- Some order types, including VWAP, Auction, RFQ and Pegged-to-Market, are unsupported.
- Combo support is limited.
- Stops and other complex orders are simulated and may behave differently from production.
- Penny fills for US options are not supported.
- Market orders without an opposite quote may be held until a quote appears.

For this reason, the guarded order tool supports **stock limit orders only**. Paper fills must be evaluated against the observed bid/ask, not treated as realistic execution evidence.

## TWS API pacing and capacity

### General request rate

The documented request rate is:

\[
\text{requests per second} = \frac{\text{maximum market-data lines}}{2}
\]

A default 100-line user therefore has a 50 requests/second limit. The in-process governor is conservative, but cannot observe requests made by TWS watchlists, another API client or another process.

### Market-data lines

- Default: 100 simultaneous market-data lines.
- TWS watchlists and API subscriptions share the same allowance.
- At 100 lines, IBKR documents approximately 5 tick-by-tick subscriptions and 3 market-depth subscriptions.
- This project reserves 20 lines by default and limits operational radar streams to 40, leaving room for TWS, positions, recovery and temporary requests.

### Historical-data pacing

Avoid all of the following:

- Identical historical requests within 15 seconds.
- Six or more requests for the same contract, exchange and tick type within two seconds.
- More than 60 historical requests in ten minutes.
- `BID_ASK` historical requests count twice.

The local governor enforces these rules only for requests routed through the new operational tools. Legacy tools and other processes remain outside its view.

### Scanner limits

- Maximum 50 returned rows per scan.
- Maximum 10 concurrent API scans.
- A scan ranking does not consume a quote line for every result, but prices must be requested separately.

### Delayed-data limits

Delayed data is normally 15–20 minutes behind and is supported only by `reqMktData` and `reqHistoricalData`. It does not substitute for tick-by-tick, depth or real-time bars. The MCP labels delayed data as `trade_eligible=false`.

## Market-data permissions

Most securities require an API-enabled Level 1 subscription. IBKR currently states that:

- The account must be fully opened and generally IBKR Pro for API market data.
- A USD 500 minimum equity requirement applies to most individual market-data/research subscription holders, in addition to subscription costs.
- The Market Data API Acknowledgement must be enabled in Client Portal.
- Forex and cryptocurrencies do not require an additional market-data subscription.
- US options require OPRA; Greeks also depend on the underlying equity/index entitlement.
- Data visible inside TWS may still be unavailable to an off-platform API request.

When live data is shared from live to paper, live and paper cannot consume the same shared feed simultaneously. Error `10197` indicates a competing session; the live session receives priority.

## News

Enable API news providers under **TWS / IB Gateway → Global Configuration → API → News Configuration**.

Officially supported API providers currently include:

- `BRFUPDN` — Briefing.com Analyst Actions, free.
- `BRFG` — Briefing.com General Market Columns, free.
- `DJNL` — Dow Jones Newsletters, free.
- `BZ` — Benzinga Breaking News via API, paid research subscription; IBKR currently lists USD 35/month.

`ibkr://news-bulletins` contains system and operational messages. It must not be used as a substitute for company-news sentiment.

## Important error codes

| Code | Meaning and response |
|---:|---|
| 10089 | API market-data subscription missing. Request delayed data or add the entitlement. |
| 10167 | Live data missing; delayed data is being displayed. Do not trade from it. |
| 10186 | Delayed data is not enabled or unavailable. |
| 10187 | Historical ticks unavailable due to permissions. |
| 10197 | Shared market data is being used by a competing live/paper session. |
| 1100 | Connectivity lost. Suspend signals and orders. |
| 1101 | Connectivity restored but market-data subscriptions were lost. Resubscribe. |
| 1102 | Connectivity restored and subscriptions were maintained. Validate freshness. |

## Official sources

- Market data subscriptions and news: https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/
- TWS API pacing and delayed-data behavior: https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/
- Paper account behavior: https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/
- Sharing live data with paper: https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/
- Third-party/API paper connection notes: https://www.interactivebrokers.com/campus/ibkr-api-page/third-party-connections/
