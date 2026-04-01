# AI Codebase Analysis Specification
Version: 1.0

This document defines how AI assistants should analyze this repository.

The goal is to ensure the AI understands the entire codebase architecture,
strategy logic, and potential issues across modules.

The AI should treat this repository as a production trading system and perform
a systematic audit.

---

# Repository Overview

This repository implements an automated trading bot.

Platform:
Polymarket

Primary market:
BTC 15-minute prediction market (UP / DOWN)

Trading style:
Maker-based market making.

The bot posts limit orders and earns spread through providing liquidity.

Strategy goals:

1. Capture bid/ask spread
2. Maintain market neutrality
3. Avoid inventory accumulation
4. Maintain positive expected PnL
5. Minimize adverse selection
6. Maintain safe risk exposure

---

# Key Components Expected in the Codebase

The repository may contain the following logical components.

AI should attempt to identify them even if naming differs.

## Market Data

Responsible for:

- fetching orderbook
- market prices
- trade events
- market state

Example modules:

market_data
orderbook
price_feed

---

## Strategy Logic

Responsible for:

- price quoting
- spread calculation
- fair value estimation
- direction bias

Example modules:

strategy
pricing
quote_engine

---

## Execution Engine

Responsible for:

- order placement
- order cancellation
- order updates
- fill handling

Example modules:

execution
order_manager
trading_engine

---

## Position Tracking

Responsible for:

- current position
- exposure
- inventory skew

Example modules:

portfolio
position_manager

---

## Risk Management

Responsible for:

- position limits
- exposure control
- circuit breakers
- stop trading conditions

Example modules:

risk
safety

---

## PnL Accounting

Responsible for:

- realized pnl
- unrealized pnl
- fee accounting
- trade settlement

Example modules:

pnl
accounting
metrics

---

## Configuration

Responsible for:

- env variables
- strategy parameters
- API credentials

Example modules:

config
settings

---

# Analysis Tasks

The AI must analyze the repository from a system-level perspective.

The AI must NOT review only individual files.
The AI must understand cross-module behavior.

---

# Task 1: Architecture Mapping

Identify:

- main entrypoints
- module responsibilities
- dependency structure
- data flow
- execution flow

Output a high-level architecture description.

---

# Task 2: Execution Flow Analysis

Trace the entire trading loop.

Startup
→ fetch market data
→ compute strategy signals
→ generate quotes
→ submit orders
→ handle fills
→ update pnl
→ run risk checks

Identify:

- dead code
- circular logic
- hidden dependencies

---

# Task 3: Strategy Correctness

Verify the strategy implements proper market making logic.

Check:

- fair price calculation
- spread logic
- quote refresh logic
- inventory skew adjustment
- maker-only order placement

Detect:

- directional bias
- latency arbitrage vulnerability
- predictable quoting

---

# Task 4: PnL Accounting Consistency

Verify that PnL calculations are consistent.

Check:

- realized pnl
- unrealized pnl
- fee deductions
- partial fills
- order cancellations

Detect:

- double counting
- missing fees
- incorrect settlement

---

# Task 5: Environment Configuration

Analyze:

.env
config files
strategy parameters

Detect:

- conflicting parameters
- unused variables
- dangerous defaults
- inconsistent naming

---

# Task 6: Risk Management

Verify that risk controls exist and function properly.

Check:

- max position
- max exposure
- inventory skew control
- stop trading conditions
- circuit breaker logic

---

# Task 7: Failure Scenarios

Analyze how the system handles:

API failure
order rejection
partial fills
network delay
market halt
stale market data

---

# Task 8: Code Maintainability

Evaluate:

- module structure
- repeated logic
- long functions
- code duplication
- logging consistency

Provide refactoring suggestions.

---

# Task 9: Trading-Specific Risk Analysis

Check for:

Inventory drift

Adverse selection

Maker fill asymmetry

Quote spam

Overtrading

---

# Required Output

The AI should produce a structured audit report containing:

1 Architecture overview
2 Execution flow diagram
3 Strategy correctness analysis
4 PnL inconsistencies
5 Config conflicts
6 Risk management flaws
7 Failure handling gaps
8 Maintainability issues
9 Refactoring suggestions
10 Priority fixes