You are a senior quant engineer and trading system auditor.

Your task is to perform a deep analysis of this repository.

The repository implements an automated trading bot for Polymarket.

Market:
BTC 15-minute prediction market.

Trading style:
maker-based market making.

Strategy goal:

- capture spread
- avoid directional exposure
- maintain positive pnl
- provide liquidity safely

You must analyze the repository as a full trading system.

Do NOT review individual files in isolation.

Focus on cross-module interactions.

Follow the analysis specification in docs/skills.md.

---

# Step 1

Build a full architecture map.

Identify:

- entrypoints
- major modules
- dependencies
- execution flow

Explain how the trading system operates end-to-end.

---

# Step 2

Trace the full execution loop.

Startup
→ market data
→ strategy calculation
→ quote generation
→ order submission
→ order fills
→ pnl update
→ risk checks

Detect hidden dependencies and logic loops.

---

# Step 3

Strategy validation.

Verify the strategy implements a valid maker market making strategy.

Check for:

- incorrect fair price logic
- spread miscalculation
- directional bias
- slow quote refresh
- predictable quotes

---

# Step 4

PnL audit.

Verify pnl calculations across modules.

Check:

- realized pnl
- unrealized pnl
- fee handling
- partial fills
- order cancellations

Identify any pnl-destroying bugs.

---

# Step 5

Configuration audit.

Review:

.env
config files

Detect:

- conflicting parameters
- unsafe defaults
- unused variables
- parameter mismatch across modules

---

# Step 6

Risk analysis.

Verify the presence of:

position limits
inventory limits
max loss
kill switches
circuit breakers

---

# Step 7

Failure scenario simulation.

Analyze behavior under:

API outage
order rejection
partial fill
network latency
stale market data

---

# Step 8

Trading-specific bug detection.

Look for:

inventory drift
adverse selection vulnerability
maker fill asymmetry
quote spam loops
overtrading

---

# Step 9

Code quality analysis.

Evaluate:

code structure
module separation
code duplication
function complexity
logging consistency

Suggest refactoring improvements.

---

# Final Output

Produce a structured audit report with sections:

Architecture overview
Execution flow
Strategy logic issues
PnL inconsistencies
Configuration conflicts
Risk management flaws
Failure scenario vulnerabilities
Maintainability issues
Recommended refactoring
Critical bugs
Priority fixes
