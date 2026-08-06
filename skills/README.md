# IBKR MCP Skills

These skills are designed for the operational tools added by the `agent/operational-agent-skills` branch.

- `ibkr-preflight`: connect and identify the real account/data capabilities.
- `ibkr-operational-radar`: run a scanner-driven intraday radar without NFF.
- `ibkr-tick-radar`: run deterministic Time & Sales signals, decay scales, and calibrated 1/3/5-minute outcomes.
- `ibkr-paper-trading`: submit tightly guarded paper stock limit orders.

Every workflow starts with preflight. No skill may treat delayed data as live or use paper fills as evidence of real execution quality. Raw ticks stay inside the deterministic MCP process; the Agent reads alerts and summarized state only.
