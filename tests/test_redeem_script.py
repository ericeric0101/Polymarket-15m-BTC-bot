from types import SimpleNamespace

import scripts.check_positions_and_redeem as redeem_script
from bot.collateral_tokens import PUSD_ADDRESS
from scripts.check_positions_and_redeem import classify_nonce_advanced_redeem


def test_advanced_nonce_with_zero_remaining_ctf_balance_is_already_redeemed():
    assert classify_nonce_advanced_redeem(
        submitted_nonce=2602,
        latest_nonce=2603,
        remaining_position_balance=0,
    ) == "already_redeemed"


def test_advanced_nonce_with_remaining_ctf_balance_requires_retry_at_new_nonce():
    assert classify_nonce_advanced_redeem(
        submitted_nonce=2602,
        latest_nonce=2603,
        remaining_position_balance=5_468_750,
    ) == "retry_with_new_nonce"


def test_nonce_not_advanced_does_not_authorize_new_nonce_retry():
    assert classify_nonce_advanced_redeem(
        submitted_nonce=2602,
        latest_nonce=2602,
        remaining_position_balance=5_468_750,
    ) == "retry_same_nonce_or_timeout"


class _Call:
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value() if callable(self.value) else self.value


class _TxFunction:
    def __init__(self):
        self.built = []

    def estimate_gas(self, _params):
        return 100_000

    def build_transaction(self, params):
        self.built.append(dict(params))
        return dict(params)


def _fake_web3(monkeypatch, *, zero_balance_after_initial_read):
    p_usd_balance_reads = {1: 0, 2: 0}
    submitted_transactions = []
    redeem_tx = _TxFunction()

    class Functions:
        def getCollectionId(self, _parent, _condition, index_set):
            return _Call(index_set)

        def getPositionId(self, collateral, collection_id):
            return _Call((collateral.lower(), collection_id))

        def balanceOf(self, _owner, position_id):
            collateral, index_set = position_id
            if collateral != PUSD_ADDRESS.lower():
                return _Call(0)
            p_usd_balance_reads[index_set] += 1
            if zero_balance_after_initial_read and p_usd_balance_reads[index_set] > 1:
                return _Call(0)
            return _Call(2_734_375)

        def redeemPositions(self, *_args):
            return redeem_tx

    contract = SimpleNamespace(functions=Functions())

    class FakeEth:
        max_priority_fee = 1
        account = SimpleNamespace(
            sign_transaction=lambda tx, private_key: SimpleNamespace(
                raw_transaction=b"signed", transaction=tx,
            ),
        )

        def __init__(self):
            self.latest_nonce = 0

        def get_transaction_count(self, _owner, block):
            return self.latest_nonce if block == "latest" else max(0, self.latest_nonce)

        def get_block(self, _tag):
            return {"baseFeePerGas": 1}

        def contract(self, address, abi):
            if address.lower() == redeem_script.CTF_ADDRESS.lower():
                return contract
            return SimpleNamespace(functions=Functions())

        def send_raw_transaction(self, raw):
            transaction = raw if isinstance(raw, dict) else None
            submitted_transactions.append(transaction)
            return SimpleNamespace(hex=lambda: f"0x{len(submitted_transactions):064x}")

    eth = FakeEth()
    web3_instance = SimpleNamespace(
        eth=eth,
        middleware_onion=SimpleNamespace(inject=lambda *_args, **_kwargs: None),
    )

    class FakeWeb3:
        HTTPProvider = staticmethod(lambda _url: None)
        to_checksum_address = staticmethod(lambda address: address)
        to_bytes = staticmethod(lambda *, hexstr: bytes.fromhex(hexstr.removeprefix("0x")))

        def __new__(cls, _provider):
            return web3_instance

    import web3
    import web3.middleware

    monkeypatch.setattr(web3, "Web3", FakeWeb3)
    monkeypatch.setattr(web3.middleware, "ExtraDataToPOAMiddleware", object())
    monkeypatch.setattr(
        redeem_script,
        "get_ctf_collateral",
        lambda: SimpleNamespace(symbol="pUSD", address=PUSD_ADDRESS),
    )
    monkeypatch.setenv("AUTO_REDEEM_MAX_SEND_ATTEMPTS", "2")
    monkeypatch.setenv("AUTO_REDEEM_RECEIPT_TIMEOUT_SEC", "30")
    monkeypatch.setattr(redeem_script.time, "sleep", lambda _seconds: None)
    return eth, redeem_tx, submitted_transactions


def test_nonce_advanced_without_receipt_reconciles_zero_position_without_resend(monkeypatch, capsys):
    eth, _redeem_tx, sent = _fake_web3(monkeypatch, zero_balance_after_initial_read=True)
    waits = iter([RuntimeError("nonce_advanced_without_receipt")])

    def wait_for_receipt(*_args, **_kwargs):
        result = next(waits)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(redeem_script, "_wait_for_receipt_with_replacement_check", wait_for_receipt)

    def send_raw_transaction(raw):
        sent.append(raw)
        eth.latest_nonce = 1
        return SimpleNamespace(hex=lambda: "0xredeem")

    monkeypatch.setattr(eth, "send_raw_transaction", send_raw_transaction)
    redeem_script._redeem_conditions(
        "private-key", "0xowner", 137, "https://rpc.invalid",
        ["0x" + "11" * 32], {"0x" + "11" * 32: 5.0},
    )

    assert len(sent) == 1
    assert "redeem reconciled without receipt" in capsys.readouterr().out


def test_nonce_advanced_with_position_remaining_retries_on_synced_nonce(monkeypatch, capsys):
    eth, redeem_tx, sent = _fake_web3(monkeypatch, zero_balance_after_initial_read=False)
    waits = iter([
        RuntimeError("nonce_advanced_without_receipt"),
        SimpleNamespace(status=1, transactionHash=SimpleNamespace(hex=lambda: "0xconfirmed")),
    ])

    def wait_for_receipt(*_args, **_kwargs):
        result = next(waits)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(redeem_script, "_wait_for_receipt_with_replacement_check", wait_for_receipt)

    def send_raw_transaction(raw):
        sent.append(raw)
        eth.latest_nonce += 1
        return SimpleNamespace(hex=lambda: f"0x{len(sent)}")

    monkeypatch.setattr(eth, "send_raw_transaction", send_raw_transaction)
    redeem_script._redeem_conditions(
        "private-key", "0xowner", 137, "https://rpc.invalid",
        ["0x" + "22" * 32], {"0x" + "22" * 32: 5.0},
    )

    assert len(sent) == 2
    assert [tx["nonce"] for tx in redeem_tx.built] == [0, 1]
    assert "retrying with fresh nonce" in capsys.readouterr().out
