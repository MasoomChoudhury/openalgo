"""No network or orders: source-clock preservation, component cache and replay."""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import threading

import pytest

spec = importlib.util.spec_from_file_location(
    "kotak_source_times",
    Path(__file__).resolve().parents[1] / "broker/kotak/streaming/source_times.py",
)
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)
NOW = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
EPOCH = int(NOW.timestamp())


def packet(*, trade=EPOCH, update=EPOCH, level=8, kind="scrip", price=10, bids=None):
    decoded = {"type": kind, "level": level, "last_trade_time": trade, "last_update_time": update}
    return st.decoded_times(decoded, received_at=NOW.isoformat()) | {
        "ltp": price,
        "open": 10,
        "high": 11,
        "low": 9,
        "prev_close": 10,
        "volume": 1000,
        "oi": 2000,
        **(
            {
                "bids": bids
                if bids is not None
                else [{"price": 9.95, "quantity": 65, "orders": 1}],
                "asks": [{"price": 10, "quantity": 65, "orders": 1}],
            }
            if level in (8, 16)
            else {}
        ),
    }


@pytest.mark.parametrize(
    "value", [None, 0, -2209008600, True, "1790765055", EPOCH * 1000, EPOCH * 1000000, float("nan")]
)
def test_missing_sentinel_and_wrong_unit_are_unknown(value):
    assert st.unix_seconds(value) is None


def test_unix_seconds_is_explicit_and_does_not_use_price_divider():
    assert st.unix_seconds(EPOCH) == NOW.isoformat()
    source = packet()
    assert source["source_time_unit"] == "unix_seconds"
    assert source["source_time_raw"] == {"last_trade_time": EPOCH, "last_update_time": EPOCH}


def test_cache_republication_advances_only_publication_clock():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    a = cache.payload("NFO", "OPT", 3, published_at=NOW.isoformat())
    b = cache.payload("NFO", "OPT", 3, published_at=(NOW + timedelta(minutes=5)).isoformat())
    for key in (
        "broker_price_time",
        "broker_depth_time",
        "server_received_at",
        "component_received_at",
    ):
        assert a[key] == b[key]
    assert a["server_published_at"] != b["server_published_at"]
    assert a["timestamp"] != b["timestamp"]


def test_price_only_update_does_not_refresh_depth():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    cache.update(
        "NFO", "OPT", packet(trade=EPOCH + 10, update=None, level=1, kind="scrip_lite", price=11)
    )
    result = cache.payload("NFO", "OPT", 3)
    assert result["broker_depth_time"] == NOW.isoformat()
    assert result["broker_price_time"] == (NOW + timedelta(seconds=10)).isoformat()
    assert result["ltp"] == 11


def test_depth_update_does_not_refresh_old_trade_clock():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    cache.update("NFO", "OPT", packet(update=EPOCH + 10))
    result = cache.payload("NFO", "OPT", 3)
    assert result["broker_price_time"] == NOW.isoformat()
    assert result["broker_depth_time"] == (NOW + timedelta(seconds=10)).isoformat()


def test_out_of_order_components_are_ignored_independently():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    cache.update("NFO", "OPT", packet(trade=EPOCH - 10, update=EPOCH + 10, price=1))
    result = cache.payload("NFO", "OPT", 3)
    assert result["ltp"] == 10
    assert result["broker_price_time"] == NOW.isoformat()
    assert result["broker_depth_time"] == (NOW + timedelta(seconds=10)).isoformat()


def test_new_price_without_clock_never_reuses_previous_clock():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    cache.update("NFO", "OPT", packet(trade=None, price=12))
    result = cache.payload("NFO", "OPT", 3)
    assert result["ltp"] == 12
    assert result["broker_price_time"] is None


def test_zero_levels_remove_old_depth_instead_of_inventing_a_current_book():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    cache.update("NFO", "OPT", packet(update=EPOCH + 10, bids=[]))
    result = cache.payload("NFO", "OPT", 3)
    assert result["depth"]["buy"] == []
    assert cache.snapshots(3)["NFO"]["OPT"]["buyBook"] == {}


def test_disconnect_and_unsubscribe_invalidate_source_cache():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    cache.discard("NFO", "OPT")
    assert cache.payload("NFO", "OPT", 3) is None
    cache.update("NFO", "OPT", packet())
    cache.clear()
    assert cache.snapshots(3) == {}


def test_snapshot_mutation_cannot_rewrite_cached_evidence():
    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    result = cache.payload("NFO", "OPT", 3)
    result["depth"]["buy"][0]["price"] = 99
    result["source_time_raw"]["depth"]["last_update_time"] = 0
    assert cache.payload("NFO", "OPT", 3)["depth"]["buy"][0]["price"] == 9.95
    assert cache.payload("NFO", "OPT", 3)["source_time_raw"]["depth"]["last_update_time"] == EPOCH


def test_sfeed_normalization_and_real_adapter_publish_have_same_source_clock():
    import websocket_proxy
    from broker.kotak.streaming.sfeed_websocket import KotakSFeedWebSocket

    client = KotakSFeedWebSocket({"sid": "unused-test-value"}, ws_url="wss://invalid", ucc="test")
    client._symbols = {"nse_fo|1": "OPT"}
    decoded = {
        "type": "scrip",
        "level": 8,
        "exchange_segment": "nse_fo",
        "instrument_token": "1",
        "last_trade_time": EPOCH,
        "last_update_time": EPOCH,
        "last_traded_price": 10,
        "buy": [{"price": 9.95, "quantity": 65, "orders": 1}],
        "sell": [{"price": 10, "quantity": 65, "orders": 1}],
    }
    normalized = client._to_depth(decoded)
    ad = websocket_proxy.KotakWebSocketAdapter.__new__(websocket_proxy.KotakWebSocketAdapter)
    ad._lock = threading.RLock()
    ad._kotak_to_openalgo = {("nse_fo", "1"): ("NFO", "OPT")}
    ad._symbol_modes = {("nse_fo", "1"): {1, 2, 3}}
    published = []
    ad.publish_market_data = lambda topic, data: published.append((topic, data))
    ad._on_data_received(normalized)
    assert len(published) == 3
    depth = next(data for topic, data in published if topic.endswith("DEPTH"))
    assert depth["broker_depth_time"] == NOW.isoformat()
    assert depth["broker_price_time"] == NOW.isoformat()
    assert depth["time_schema_version"] == 1
    # Base/proxy serialization is transparent to the contract.
    assert json.loads(json.dumps(depth))["source_time_raw"]["depth"]["last_update_time"] == EPOCH
    ad.cleanup = lambda: None  # bare fixture owns no broker/ZMQ resources


def test_index_has_a_price_clock_but_no_invented_book_clock():
    cache = st.SourceCache()
    cache.update("NSE_INDEX", "NIFTY", packet(kind="index", level=0))
    result = cache.payload("NSE_INDEX", "NIFTY", 3)
    assert result["broker_price_time"] == NOW.isoformat()
    assert result["broker_depth_time"] is None
    assert result["depth"] == {"buy": [], "sell": []}


@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("disconnected", [False, True])
def test_actual_zmq_and_proxy_boundaries_preserve_component_clocks(
    monkeypatch, pooled, disconnected
):
    import asyncio
    import logging
    from types import SimpleNamespace
    from websocket_proxy.base_adapter import BaseBrokerWebSocketAdapter
    from websocket_proxy.connection_manager import SharedZmqPublisher
    from websocket_proxy.server import WebSocketProxy
    import websocket_proxy.server as server

    cache = st.SourceCache()
    cache.update("NFO", "OPT", packet())
    expected = cache.payload("NFO", "OPT", 3)
    if disconnected:
        expected = cache.disconnect_payloads()[0][2]
    frames = []
    socket = SimpleNamespace(send_multipart=frames.append)
    publisher = SharedZmqPublisher.__new__(SharedZmqPublisher)
    publisher._connected = True
    publisher._publish_lock = threading.Lock()
    publisher.socket = socket
    publisher.logger = logging.getLogger("replay")
    publisher.cleanup = lambda: None
    adapter = SimpleNamespace(
        _uses_shared_zmq=pooled, _shared_publisher=publisher, socket=socket, logger=publisher.logger
    )
    BaseBrokerWebSocketAdapter.publish_market_data(adapter, "NFO_OPT_DEPTH", expected)
    assert len(frames) == 1
    wire = []
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.running = True
    proxy._cleanup_stale_throttle_entries = lambda: None
    proxy._log_stale_adapters = lambda: None
    proxy.subscription_index = {("OPT", "NFO", 3): {"client"}}
    proxy.last_message_time = {}
    proxy.last_tick_time = {}
    proxy.user_mapping = {"client": "replay"}
    proxy.user_broker_mapping = {"replay": "kotak"}
    proxy._messages_processed = 0

    async def receive():
        proxy.running = False  # deliver exactly one frame, no background task
        return frames[0]

    async def send(message):
        wire.append(json.loads(message))

    proxy.socket = SimpleNamespace(recv_multipart=receive)
    proxy.clients = {"client": SimpleNamespace(send=send)}
    pricing_ticks = []
    monkeypatch.setattr(
        server,
        "get_market_data_service",
        lambda: SimpleNamespace(process_market_data=pricing_ticks.append),
    )
    asyncio.run(proxy.zmq_listener())
    assert len(wire) == 1
    assert wire[0]["data"] == expected
    assert wire[0]["broker"] == "kotak"
    assert wire[0]["mode"] == 3
    assert len(pricing_ticks) == (0 if disconnected else 1)


def test_adapter_polling_getters_preserve_source_clock_and_empty_levels():
    from websocket_proxy import KotakWebSocketAdapter

    adapter = KotakWebSocketAdapter.__new__(KotakWebSocketAdapter)
    adapter._lock = threading.RLock()
    adapter._source_cache = st.SourceCache()
    adapter._ltp_cache = {}
    adapter._quote_cache = {}
    adapter._depth_cache = {}
    adapter._depth_poll_state = {}
    adapter._ws_client = None
    adapter.cleanup = lambda: None
    adapter._source_cache.update("NFO", "OPT", packet(bids=[]))
    for getter in (adapter.get_ltp, adapter.get_quote, adapter.get_depth, adapter.get_last_depth):
        data = getter()["NFO"]["OPT"]
        assert data["broker_price_time"] == NOW.isoformat()
        assert data["server_received_at"] == NOW.isoformat()
        if "buyBook" in data:
            assert data["buyBook"] == {}
    assert adapter.get_last_quote()[("NFO", "OPT")]["broker_price_time"] == NOW.isoformat()


def test_export_manifest_revision_matches_exact_copied_sources(tmp_path):
    import hashlib

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "export_test", root / "scripts/export_kotak_timestamp_audit.py"
    )
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    result = exporter.export(
        root,
        tmp_path,
        server_version="replay-only",
        reviewed_by="fixture",
        explanation="synthetic test, not a deployment certificate",
    )
    hashes = sorted(
        [
            {
                "name": item["path"],
                "sha256": hashlib.sha256((tmp_path / item["path"]).read_bytes()).hexdigest(),
            }
            for item in result["sources"]
        ],
        key=lambda item: item["name"],
    )
    assert (
        result["adapter_revision"]
        == "kotak-sfeed-time-v1:"
        + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    )
    assert len(hashes) == 7
    with pytest.raises(FileExistsError):
        exporter.export(
            root, tmp_path, server_version="replay", reviewed_by="test", explanation="fixture"
        )


def test_adapter_disconnect_notifies_proxy_and_ignores_late_source_packets():
    from websocket_proxy import KotakWebSocketAdapter

    adapter = KotakWebSocketAdapter.__new__(KotakWebSocketAdapter)
    adapter._lock = threading.RLock()
    adapter._connected = True
    adapter._source_cache = st.SourceCache()
    adapter._source_cache.update("NFO", "OPT", packet())
    adapter._kotak_to_openalgo = {("nse_fo", "1"): ("NFO", "OPT")}
    adapter._symbol_modes = {("nse_fo", "1"): {3}}
    messages = []
    adapter.publish_market_data = lambda topic, data: messages.append((topic, data))
    adapter.cleanup = lambda: None
    adapter._invalidate_sfeed_connection()
    assert not adapter._source_cache.records
    assert messages[0][0] == "NFO_OPT_DEPTH"
    invalid = messages[0][1]
    assert invalid["source_connection_state"] == "disconnected"
    assert invalid["broker_price_time"] is None
    assert invalid["broker_depth_time"] is None
    assert invalid["ltp"] == 10  # notice must never inject a zero price tick
    adapter._on_data_received(packet() | {"e": "nse_fo", "tk": "1"})
    assert not adapter._source_cache.records
    assert len(messages) == 1


def test_previous_client_callbacks_are_fenced_after_reconnect():
    from types import SimpleNamespace
    from websocket_proxy import KotakWebSocketAdapter

    adapter = KotakWebSocketAdapter.__new__(KotakWebSocketAdapter)
    callbacks = {}
    adapter._ws_client = SimpleNamespace(set_callbacks=lambda **values: callbacks.update(values))
    calls = []
    adapter._on_data_received = calls.append
    adapter.cleanup = lambda: None
    adapter._setup_internal_callbacks()
    adapter._ws_client = SimpleNamespace()  # new connection generation
    callbacks["on_quote"](packet())
    callbacks["on_depth"](packet())
    callbacks["on_open"]()
    callbacks["on_close"]()
    callbacks["on_error"]("late old error")
    assert calls == []
