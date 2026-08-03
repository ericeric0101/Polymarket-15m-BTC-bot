from py_clob_client_v2.client import *  # noqa: F401,F403
from py_clob_client_v2.client import ClobClient as V2ClobClient
from py_clob_client_v2.clob_types import ApiCreds
from py_clob_client_v2.clob_types import BalanceAllowanceParams
from py_clob_client_v2.clob_types import MarketOrderArgs as MarketOrderArgsV2
from py_clob_client_v2.clob_types import OpenOrderParams
from py_clob_client_v2.clob_types import OrderArgs as OrderArgsV2
from py_clob_client_v2.clob_types import OrderPayload
from py_clob_client_v2.clob_types import PartialCreateOrderOptions
from py_clob_client_v2.clob_types import TradeParams

OrderArgs = OrderArgsV2
MarketOrderArgs = MarketOrderArgsV2


class ClobClient(V2ClobClient):
    def get_orders(self, params=None):
        return self.get_open_orders(params=params)

    def cancel(self, order_id: str):
        return self.cancel_order(OrderPayload(orderID=order_id))

    def create_or_derive_api_creds(self):
        try:
            return self.create_api_key()
        except Exception:
            return self.derive_api_key()
