"""SFeed source clocks and component cache. Local publication is not quote time.

The native_batch decoder returns Unix seconds, independently of price dividers.
Keep its raw integers and null out unavailable/sentinel/wrong-unit values; never
infer milliseconds from magnitude or fall back to time.time(). Deployment must
still be audited against captured broker packets before a consumer trusts this.
"""

from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import logging
import os
from pathlib import Path
import threading
import time

TIME_SCHEMA_VERSION = 1
_diagnostic_lock = threading.Lock()
_diagnostic_last = OrderedDict()
SOURCE_FILES = (
    "broker/kotak/streaming/kotak_adapter.py",
    "broker/kotak/streaming/sfeed_websocket.py",
    "broker/kotak/streaming/sfeed_protocol.py",
    "broker/kotak/streaming/source_times.py",
    "websocket_proxy/base_adapter.py",
    "websocket_proxy/connection_manager.py",
    "websocket_proxy/server.py",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def time_diagnostics(boundary, data):
    """Opt-in, bounded, sampled time-only logs; never log control/auth frames."""
    if os.getenv("KOTAK_TIME_DIAGNOSTICS", "").lower() != "true":
        return
    instrument = str(data.get("instrument_token", data.get("symbol", "")))
    key = (boundary, instrument)
    with _diagnostic_lock:
        clock = time.monotonic()
        if clock - _diagnostic_last.get(key, -30) < 30:
            return
        _diagnostic_last[key] = clock
        _diagnostic_last.move_to_end(key)
        while len(_diagnostic_last) > 256:
            _diagnostic_last.popitem(last=False)
    fields = (
        "type",
        "source_packet_type",
        "exchange_segment",
        "exchange",
        "symbol",
        "instrument_token",
        "level",
        "source_packet_level",
        "last_trade_time",
        "last_update_time",
        "last_update_time_raw",
        "broker_price_time",
        "broker_depth_time",
        "broker_last_trade_time",
        "broker_quote_time",
        "source_time_raw",
        "source_time_unit",
        "time_schema_version",
        "adapter_revision",
        "_server_received_at",
        "server_received_at",
        "server_published_at",
        "component_received_at",
        "source_connection_state",
    )
    payload = {key: data[key] for key in fields if key in data}
    logging.getLogger(__name__).info(
        "KOTAK_TIME_AUDIT %s", json.dumps({"boundary": boundary, "fields": payload})
    )


def unix_seconds(value):
    # SFeed's blank/sentinel values include zero and negative Unix seconds.
    # Contemporary clocks fit this explicit seconds range. Millis/us/ns values
    # fail rather than being guessed into plausible dates.
    if isinstance(value, bool) or not isinstance(value, int) or not 946684800 <= value < 4102444800:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


@lru_cache(maxsize=1)
def adapter_revision():
    root = Path(__file__).resolve().parents[3]
    hashes = sorted(
        (
            {
                "name": Path(name).name,
                "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            }
            for name in SOURCE_FILES
        ),
        key=lambda item: item["name"],
    )
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return "kotak-sfeed-time-v1:" + digest


def decoded_times(decoded, *, received_at=None):
    kind = decoded.get("type")
    trade_raw = decoded.get("last_trade_time")
    update_raw = decoded.get("last_update_time_raw", decoded.get("last_update_time"))
    trade = unix_seconds(trade_raw)
    update = unix_seconds(update_raw)
    depth = update if kind == "scrip" and decoded.get("level") in (8, 16) else None
    return {
        "_kotak_sfeed": kind,
        "time_schema_version": TIME_SCHEMA_VERSION,
        "adapter_revision": adapter_revision(),
        "timestamp_origin": "kotak_sfeed_source",
        "source_time_raw": {"last_trade_time": trade_raw, "last_update_time": update_raw},
        "source_time_unit": "unix_seconds",
        "source_packet_type": kind,
        "source_packet_level": decoded.get("level"),
        "broker_price_time": trade,
        "broker_last_trade_time": trade,
        "broker_quote_time": update,
        "broker_depth_time": depth,
        "server_received_at": received_at or utc_now(),
    }


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except ValueError:
        return None


def _accept(component, field, incoming):
    old = _time(component.get(field)) if component else None
    new = _time(incoming.get(field))
    # A value with no usable clock is retained as unknown, never stamped with
    # a previously valid clock. Only known older packets can be ignored.
    return not (old and new and new < old)


def _positive(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


class SourceCache:
    """Value+clock pairs per instrument. Adapter owns synchronization."""

    def __init__(self):
        self.records = {}

    def clear(self):
        self.records.clear()

    def discard(self, exchange, symbol):
        self.records.pop((exchange, symbol), None)

    def update(self, exchange, symbol, incoming):
        record = self.records.setdefault((exchange, symbol), {})
        kind = incoming.get("_kotak_sfeed")
        meta = {
            key: deepcopy(value)
            for key, value in incoming.items()
            if key.startswith("broker_")
            or key
            in {
                "time_schema_version",
                "adapter_revision",
                "timestamp_origin",
                "source_time_raw",
                "source_time_unit",
                "source_packet_type",
                "source_packet_level",
                "server_received_at",
            }
        }
        changed = False
        if _positive(incoming.get("ltp")) and _accept(
            record.get("price"), "broker_price_time", meta
        ):
            record["price"] = meta | {"ltp": incoming["ltp"]}
            changed = True
        if kind in {"index", "scrip"}:
            clock = "broker_price_time" if kind == "index" else "broker_quote_time"
            if _accept(record.get("quote"), clock, meta):
                record["quote"] = meta | {
                    key: incoming.get(source, 0)
                    for key, source in {
                        "open": "open",
                        "high": "high",
                        "low": "low",
                        "close": "prev_close",
                        "volume": "volume",
                        "oi": "oi",
                    }.items()
                }
                changed = True
        if kind == "scrip" and "bids" in incoming and "asks" in incoming:
            if _accept(record.get("depth"), "broker_depth_time", meta):
                # SFeed sends complete snapshots. Zero rows delete levels;
                # carrying previous rows forward would invent a current book.
                record["depth"] = meta | {
                    "depth": {
                        "buy": deepcopy(incoming["bids"]),
                        "sell": deepcopy(incoming["asks"]),
                    },
                    "totalbuyqty": incoming.get("totalbuyqty", 0),
                    "totalsellqty": incoming.get("totalsellqty", 0),
                }
                changed = True
        return changed

    def payload(self, exchange, symbol, mode, *, published_at=None):
        record = self.records.get((exchange, symbol), {})
        price, quote, depth = (record.get(key, {}) for key in ("price", "quote", "depth"))
        if not price and not quote and not depth:
            return None
        if exchange.endswith("_INDEX") and not price:
            return None
        if mode == 3 and not depth and not exchange.endswith("_INDEX"):
            return None
        relevant = depth if mode == 3 and depth else quote if mode == 2 and quote else price
        publish = published_at or utc_now()
        result = {
            "symbol": symbol,
            "exchange": exchange,
            "ltp": price.get("ltp", 0),
            # Legacy fields remain server clocks for compatibility. New fields
            # and explicit provenance are what audited consumers must use.
            "timestamp": int(datetime.fromisoformat(publish).timestamp() * 1000),
            "timestamp_origin": "server_publication",
            "ltt": int(_time(price["broker_last_trade_time"]).timestamp() * 1000)
            if _time(price.get("broker_last_trade_time"))
            else int(datetime.fromisoformat(publish).timestamp() * 1000),
            "ltt_origin": "broker_last_trade"
            if _time(price.get("broker_last_trade_time"))
            else "server_publication",
            "time_schema_version": TIME_SCHEMA_VERSION,
            "adapter_revision": adapter_revision(),
            "broker_price_time": price.get("broker_price_time"),
            "broker_last_trade_time": price.get("broker_last_trade_time"),
            "broker_quote_time": quote.get("broker_quote_time"),
            "broker_depth_time": depth.get("broker_depth_time"),
            "server_received_at": relevant.get("server_received_at"),
            "server_published_at": publish,
            "component_received_at": {
                key: record.get(key, {}).get("server_received_at")
                for key in ("price", "quote", "depth")
            },
            "source_time_raw": {
                key: deepcopy(record.get(key, {}).get("source_time_raw"))
                for key in ("price", "quote", "depth")
            },
            "source_time_unit": "unix_seconds",
            "source_packet_type": relevant.get("source_packet_type"),
            "source_connection_state": "connected",
        }
        if mode >= 2:
            result.update(
                {key: quote.get(key, 0) for key in ("open", "high", "low", "close", "volume", "oi")}
            )
        if mode == 3:
            result.update(
                {
                    "depth": deepcopy(depth.get("depth", {"buy": [], "sell": []})),
                    "totalbuyqty": depth.get("totalbuyqty", 0),
                    "totalsellqty": depth.get("totalsellqty", 0),
                }
            )
        return result

    def disconnect_payloads(self):
        """Invalidate consumers even if their proxy transport stays connected.

        Preserve display values but null their clocks. These are control
        notices, not price ticks, and must bypass MarketDataService pricing.
        """
        outputs = []
        for exchange, symbol in self.records:
            data = self.payload(exchange, symbol, 3) or self.payload(exchange, symbol, 2)
            if data is None:
                continue
            for field in (
                "broker_price_time",
                "broker_last_trade_time",
                "broker_quote_time",
                "broker_depth_time",
            ):
                data[field] = None
            data["source_connection_state"] = "disconnected"
            data["source_time_raw"] = {key: None for key in ("price", "quote", "depth")}
            data["component_received_at"] = {key: None for key in ("price", "quote", "depth")}
            data["server_received_at"] = None
            data["ltt"] = data["timestamp"]
            data["ltt_origin"] = "server_publication"
            outputs.append((exchange, symbol, data))
        return outputs

    def snapshots(self, mode):
        result = {}
        for exchange, symbol in self.records:
            payload = self.payload(exchange, symbol, mode)
            if payload is None:
                continue
            if mode == 3:
                # Preserve the existing polling shape alongside the streaming
                # ladder. Do not merge zero rows with a previous book.
                for side, key in (("buy", "buyBook"), ("sell", "sellBook")):
                    payload[key] = {
                        str(i): {
                            "price": str(row.get("price", 0)),
                            "qty": str(row.get("quantity", 0)),
                            "orders": str(row.get("orders", 0)),
                        }
                        for i, row in enumerate(payload["depth"][side][:5], 1)
                    }
            result.setdefault(exchange, {})[symbol] = payload
        return result
