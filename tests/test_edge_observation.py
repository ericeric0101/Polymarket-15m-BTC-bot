from bot.quote_service import should_emit_edge_observation


def test_edge_observation_throttle_emits_on_signature_change_or_interval():
    signatures = {}
    timestamps = {}

    assert should_emit_edge_observation("inst-up", ("a",), 10.0, signatures, timestamps, 1.0) is True
    assert should_emit_edge_observation("inst-up", ("a",), 10.5, signatures, timestamps, 1.0) is False
    assert should_emit_edge_observation("inst-up", ("b",), 10.5, signatures, timestamps, 1.0) is True
    assert should_emit_edge_observation("inst-up", ("b",), 11.6, signatures, timestamps, 1.0) is True


def test_edge_observation_throttle_is_instrument_specific():
    signatures = {}
    timestamps = {}

    assert should_emit_edge_observation("inst-up", ("a",), 10.0, signatures, timestamps, 1.0) is True
    assert should_emit_edge_observation("inst-down", ("a",), 10.0, signatures, timestamps, 1.0) is True
