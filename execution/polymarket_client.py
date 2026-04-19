"""
Polymarket Client - Production Implementation
Real API integration with Polymarket CLOB
"""
import os
import asyncio
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from loguru import logger
import httpx

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderPayload,
    OrderType as PolyOrderType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL
POLYMARKET_AVAILABLE = True


class PolymarketClient:
    """
    Production Polymarket API client.
    
    Features:
    - Real order placement
    - Live market data
    - Position tracking
    - Balance management
    """
    
    def __init__(
        self,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        chain_id: int = 137,  # Polygon mainnet
        testnet: bool = False,
    ):
        """
        Initialize Polymarket client.
        
        Args:
            private_key: Ethereum private key (without 0x prefix)
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            api_passphrase: Polymarket API passphrase
            chain_id: 137 for Polygon mainnet, 80002 for Amoy testnet
            testnet: Use testnet mode
        """
        # Load from environment if not provided
        self.private_key = private_key or os.getenv("POLYMARKET_PK")
        self.api_key = api_key or os.getenv("POLYMARKET_API_KEY")
        self.api_secret = api_secret or os.getenv("POLYMARKET_API_SECRET")
        self.api_passphrase = api_passphrase or os.getenv("POLYMARKET_PASSPHRASE")
        self.funder = (
            os.getenv("POLYMARKET_FUNDER")
            or os.getenv("POLYMARKET_WALLET_ADDRESS")
            or os.getenv("WALLET_ADDRESS")
        )
        
        self.chain_id = chain_id
        self.testnet = testnet
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        
        # Client instance
        self.client: Optional[ClobClient] = None
        self._connected = False
        
        # Market cache
        self._markets_cache: Dict[str, Any] = {}
        
        # Check if SDK available
        if not POLYMARKET_AVAILABLE:
            logger.error("Polymarket SDK not available. Install: pip install py-clob-client-v2")
            return
        
        # Validate credentials
        if not self.private_key:
            logger.error("POLYMARKET_PK not found in environment")
        if not (self.api_key and self.api_secret and self.api_passphrase):
            logger.warning("L2 API credentials not fully set; will try create/derive from POLYMARKET_PK")
        
        mode = "TESTNET" if testnet else "MAINNET"
        logger.info(f"Initialized Polymarket Client [{mode}] Chain ID: {chain_id}")

    def _credential_probe_params(self) -> BalanceAllowanceParams:
        return BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=self.signature_type,
        )

    def _set_client_creds(self, creds: ApiCreds) -> None:
        self.api_key = creds.api_key
        self.api_secret = creds.api_secret
        self.api_passphrase = creds.api_passphrase
        self.client.set_api_creds(creds)

    def _ensure_valid_api_creds(self) -> None:
        if not self.client:
            raise RuntimeError("CLOB client is not initialized")

        env_creds_present = bool(self.api_key and self.api_secret and self.api_passphrase)
        if env_creds_present:
            self.client.set_api_creds(ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            ))
            try:
                self.client.get_balance_allowance(self._credential_probe_params())
                return
            except Exception as exc:
                logger.warning(f"Configured L2 creds rejected, falling back to derive/create: {exc}")

        try:
            derived = self.client.create_api_key()
            logger.info("Derived fresh L2 creds via create_api_key()")
        except Exception:
            derived = self.client.derive_api_key()
            logger.info("Derived fresh L2 creds via derive_api_key()")

        if isinstance(derived, dict):
            derived = ApiCreds(
                api_key=derived["api_key"],
                api_secret=derived["api_secret"],
                api_passphrase=derived["api_passphrase"],
            )
        self._set_client_creds(derived)

    def _post_order_with_reauth_and_retry(
        self,
        signed_order: Any,
        *,
        order_type: str,
        side: str,
        max_attempts: int = 2,
    ) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self.client.post_order(signed_order, order_type=order_type)
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                lower = msg.lower()
                if "unauthorized/invalid api key" in lower or "invalid api key" in lower:
                    logger.warning("Order post rejected by invalid L2 creds; re-deriving credentials and retrying")
                    self._ensure_valid_api_creds()
                    continue
                if (
                    side.lower() == "buy"
                    and "not enough balance / allowance" in lower
                    and attempt < max_attempts
                ):
                    logger.warning(
                        "Order post hit transient balance/allowance rejection; waiting briefly and retrying once"
                    )
                    time.sleep(1.0)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Order post failed without a concrete exception")
    
    async def connect(self) -> bool:
        """
        Connect to Polymarket API.
        
        Returns:
            True if connected successfully
        """
        if not POLYMARKET_AVAILABLE:
            logger.error("Cannot connect: SDK not installed")
            return False
        
        if not self.private_key:
            logger.error("Cannot connect: Missing private key")
            return False
        
        try:
            # Initialize CLOB client
            clob_host = os.getenv(
                "POLYMARKET_CLOB_BASE_URL",
                "https://clob-v2.polymarket.com" if self.testnet else "https://clob.polymarket.com",
            )
            self.client = ClobClient(
                clob_host,
                self.chain_id,
                key=self.private_key,
                signature_type=self.signature_type,
                funder=self.funder,
            )

            self._ensure_valid_api_creds()
            
            # Test connection
            balance = await self._get_balance_internal()
            
            if balance is not None:
                self._connected = True
                logger.info(f"✓ Connected to Polymarket CLOB")
                logger.info(f"  Balance: ${balance.get('USDC', 0):.2f} USDC")
                return True
            else:
                logger.error("Failed to verify connection")
                return False
                
        except Exception as e:
            logger.error(f"Failed to connect to Polymarket: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from API."""
        self._connected = False
        self.client = None
        logger.info("Disconnected from Polymarket")
    
    async def get_btc_market(self) -> Optional[Dict[str, Any]]:
        """
        Get BTC prediction market details.
        
        Returns:
            Market information dict
        """
        try:
            api_base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
            timeout = float(os.getenv("GAMMA_DISCOVERY_TIMEOUT_SEC", "8"))
            now = datetime.now(timezone.utc)
            interval_start = int(now.timestamp() // 900) * 900

            # Probe current + near-future BTC 15m slugs; return first existing market.
            candidates = [f"btc-updown-15m-{interval_start + (i * 900)}" for i in range(0, 4)]
            async with httpx.AsyncClient(timeout=timeout) as client:
                for slug in candidates:
                    resp = await client.get(
                        f"{api_base}/markets",
                        params={
                            "active": "true",
                            "closed": "false",
                            "archived": "false",
                            "slug": slug,
                            "limit": 1,
                        },
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    if not isinstance(payload, list) or len(payload) == 0:
                        continue
                    m = payload[0]
                    return {
                        "condition_id": m.get("conditionId") or m.get("condition_id"),
                        "market_id": m.get("id") or m.get("marketId"),
                        "question": m.get("question"),
                        "slug": m.get("slug"),
                        "end_date": m.get("endDate") or m.get("endDateIso"),
                        "raw": m,
                    }

            logger.warning("No active BTC 15m market found from Gamma")
            return None
            
        except Exception as e:
            logger.error(f"Error fetching BTC market: {e}")
            return None
    
    async def get_market_price(self, token_id: str) -> Optional[Decimal]:
        """
        Get current market price for a token.
        
        Args:
            token_id: Token ID (outcome token)
            
        Returns:
            Current price (0-1 for binary markets)
        """
        if not self.client:
            return None
        
        try:
            # Get order book
            book = self.client.get_order_book(token_id)
            
            if book and "bids" in book and len(book["bids"]) > 0:
                # Best bid price
                best_bid = Decimal(str(book["bids"][0]["price"]))
                return best_bid
            
            return None
            
        except Exception as e:
            logger.error(f"Error fetching market price: {e}")
            return None
    
    async def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order book for token.
        
        Args:
            token_id: Token ID
            
        Returns:
            Order book with bids and asks
        """
        if not self.client:
            return None
        
        try:
            book = self.client.get_order_book(token_id)
            
            return {
                "timestamp": datetime.now(),
                "token_id": token_id,
                "bids": [
                    {
                        "price": Decimal(str(bid["price"])),
                        "size": Decimal(str(bid["size"])),
                    }
                    for bid in book.get("bids", [])
                ],
                "asks": [
                    {
                        "price": Decimal(str(ask["price"])),
                        "size": Decimal(str(ask["size"])),
                    }
                    for ask in book.get("asks", [])
                ],
            }
            
        except Exception as e:
            logger.error(f"Error fetching orderbook: {e}")
            return None
    
    async def place_order(
        self,
        token_id: str,
        side: str,  # "buy" or "sell"
        size: Decimal,
        price: Optional[Decimal] = None,
        order_type: str = "GTC",  # GTC, FOK, GTD
    ) -> Optional[str]:
        """
        Place order on market.
        
        Args:
            token_id: Token ID to trade
            side: "buy" or "sell"
            size: Order size (number of outcome tokens)
            price: Limit price (0-1 range), None for market order
            order_type: Order type (GTC, FOK, GTD)
            
        Returns:
            Order ID if successful
        """
        if not self.client:
            logger.error("Client not connected")
            return None
        
        try:
            # Convert to Polymarket format
            poly_side = BUY if side.lower() == "buy" else SELL
            
            # If no price specified, use market order (best available price)
            if price is None:
                # Get best price from orderbook
                book = await self.get_orderbook(token_id)
                if not book:
                    logger.error("Cannot get market price")
                    return None
                
                if side.lower() == "buy":
                    price = book["asks"][0]["price"] if book["asks"] else Decimal("0.5")
                else:
                    price = book["bids"][0]["price"] if book["bids"] else Decimal("0.5")
            
            # Create order arguments
            builder_code = os.getenv(
                "POLY_BUILDER_CODE",
                "0x0000000000000000000000000000000000000000000000000000000000000000",
            )
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=poly_side,
                expiration=0,
                builder_code=builder_code,
                metadata="0x0000000000000000000000000000000000000000000000000000000000000000",
            )
            
            # Build and sign order
            signed_order = self.client.create_order(order_args)
            
            # Submit order
            response = self._post_order_with_reauth_and_retry(
                signed_order,
                order_type=order_type,
                side=side,
            )
            
            if response and "orderID" in response:
                order_id = response["orderID"]
                
                logger.info(
                    f"Order placed: {order_id} "
                    f"{side.upper()} {size} @ {price:.4f}"
                )
                
                return order_id
            else:
                logger.error(f"Order placement failed: {response}")
                return None
                
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel order.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if cancelled successfully
        """
        if not self.client:
            return False
        
        try:
            response = self.client.cancel_order(OrderPayload(orderID=order_id))
            
            if response:
                logger.info(f"Order cancelled: {order_id}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False
    
    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all open orders.
        
        Returns:
            List of open orders
        """
        if not self.client:
            return []
        
        try:
            orders = self.client.get_open_orders()
            
            open_orders = []
            for order in orders:
                if order.get("status") == "live":
                    open_orders.append({
                        "order_id": order["id"],
                        "token_id": order["token_id"],
                        "side": order["side"],
                        "price": Decimal(str(order["price"])),
                        "size": Decimal(str(order["size"])),
                        "filled": Decimal(str(order.get("size_matched", 0))),
                        "timestamp": datetime.fromisoformat(order["created_at"]),
                    })
            
            return open_orders
            
        except Exception as e:
            logger.error(f"Error fetching open orders: {e}")
            return []
    
    async def get_positions(self) -> List[Dict[str, Any]]:
        """
        Get current positions.
        
        Returns:
            List of positions
        """
        if not self.client:
            return []
        
        logger.warning(
            "get_positions() is not implemented against py-clob-client-v2 because the SDK "
            "does not expose a generic all-token balance endpoint. Returning an empty list."
        )
        return []
    
    async def _get_balance_internal(self) -> Optional[Dict[str, Decimal]]:
        """Internal method to get balance."""
        if not self.client:
            return None
        
        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.signature_type,
                )
            )
            raw_balance = int((result or {}).get("balance", "0"))
            balance = Decimal(str(raw_balance)) / Decimal("1000000")
            return {
                "USDC": balance,
                "pUSD": balance,
            }
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return None
    
    async def get_balance(self) -> Dict[str, Decimal]:
        """
        Get account balance.
        
        Returns:
            Balance dict with USDC and token balances
        """
        return await self._get_balance_internal() or {}
    
    async def get_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent trades.
        
        Args:
            limit: Maximum trades to return
            
        Returns:
            List of recent trades
        """
        if not self.client:
            return []
        
        try:
            trades = self.client.get_trades()
            
            recent_trades = []
            for trade in trades[:limit]:
                recent_trades.append({
                    "trade_id": trade["id"],
                    "order_id": trade["order_id"],
                    "token_id": trade["asset_id"],
                    "side": trade["side"],
                    "price": Decimal(str(trade["price"])),
                    "size": Decimal(str(trade["size"])),
                    "timestamp": datetime.fromisoformat(trade["timestamp"]),
                })
            
            return recent_trades
            
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            return []
    
    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected and self.client is not None


# Singleton instance
_polymarket_client_instance = None

def get_polymarket_client(
    testnet: bool = False,
    force_new: bool = False,
) -> PolymarketClient:
    """
    Get singleton Polymarket client.
    
    Args:
        testnet: Use testnet mode
        force_new: Force creation of new instance
    """
    global _polymarket_client_instance
    
    if _polymarket_client_instance is None or force_new:
        _polymarket_client_instance = PolymarketClient(testnet=testnet)
    
    return _polymarket_client_instance
