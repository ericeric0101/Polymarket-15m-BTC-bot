from __future__ import annotations

import os
from dataclasses import dataclass


USDCE_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
COLLATERAL_ONRAMP_ADDRESS = "0x93070a847efEf7F70739046A929D47a521F5B8ee"


@dataclass(frozen=True)
class CtfCollateral:
    symbol: str
    address: str

    @property
    def is_usdce(self) -> bool:
        return self.symbol == "USDC.e"

    @property
    def is_pusd(self) -> bool:
        return self.symbol == "pUSD"


def _normalize_collateral_token(value: str | None) -> str:
    token = (value or "").strip().lower().replace("_", "").replace("-", "")
    if token in {"", "pusd", "polymarketusd"}:
        return "PUSD"
    if token in {"usdce", "usdc.e", "usdc", "polygonusdc"}:
        return "USDCE"
    if token.startswith("0x") and len(token) == 42:
        return "ADDRESS"
    raise ValueError(
        "Unsupported POLYMARKET_CTF_COLLATERAL_TOKEN. "
        "Use PUSD, USDCE, or set POLYMARKET_CTF_COLLATERAL_ADDRESS."
    )


def get_ctf_collateral() -> CtfCollateral:
    """Return the collateral token passed to CTF redeem/merge calls.

    V2 Polymarket markets use pUSD as CTF collateral. USDC.e remains the
    underlying asset for onramp wrapping, but should not be passed to CTF calls
    unless explicitly running a legacy market flow.
    """

    explicit_address = os.getenv("POLYMARKET_CTF_COLLATERAL_ADDRESS", "").strip()
    if explicit_address:
        return CtfCollateral(symbol="custom", address=explicit_address)

    token_kind = _normalize_collateral_token(os.getenv("POLYMARKET_CTF_COLLATERAL_TOKEN", "PUSD"))
    if token_kind == "PUSD":
        return CtfCollateral(symbol="pUSD", address=PUSD_ADDRESS)
    if token_kind == "USDCE":
        return CtfCollateral(symbol="USDC.e", address=USDCE_ADDRESS)

    token_value = os.getenv("POLYMARKET_CTF_COLLATERAL_TOKEN", "").strip()
    return CtfCollateral(symbol="custom", address=token_value)
