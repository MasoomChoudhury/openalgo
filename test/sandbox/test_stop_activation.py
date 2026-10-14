"""Offline stop activation replay; synthetic orders and recorded price observations.

The SELL prices reproduce the October 14 defect, not every historical Analyzer
tick. No broker transport, authentication or strategy runtime is constructed.
"""
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from database.sandbox_db import SandboxOrders, SandboxTrades, db_session
from sandbox.execution_engine import ExecutionEngine
from sandbox.websocket_execution_engine import WebSocketExecutionEngine

STRATEGY = "stop-activation-replay"


@pytest.fixture
def engine(monkeypatch):
    value = ExecutionEngine()
    monkeypatch.setattr(value, "_update_position", lambda *args: None)
    monkeypatch.setattr(value, "_publish_fill_event", lambda **kwargs: None)
    monkeypatch.setattr(value, "_publish_order_update_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(value, "_check_pending_gtts", lambda: None)
    yield value
    db_session.rollback()
    SandboxTrades.query.filter_by(strategy=STRATEGY).delete()
    SandboxOrders.query.filter_by(strategy=STRATEGY).delete()
    db_session.commit()
    db_session.remove()


def order(action="SELL", status="trigger pending", kind="SL"):
    row = SandboxOrders(
        orderid=uuid4().hex, user_id="offline-stop-replay", strategy=STRATEGY,
        symbol="RELIANCE", exchange="NSE", product="MIS", action=action,
        quantity=1, pending_quantity=1, filled_quantity=0,
        price=Decimal("146.95" if action == "SELL" else "147.15"),
        trigger_price=Decimal("147.05"), price_type=kind, order_status=status,
        margin_blocked=Decimal("0"),
    )
    db_session.add(row)
    db_session.commit()
    return row


def deliver(engine, row, price, path):
    quote = {"ltp": price, "bid": price, "ask": price}
    if path == "websocket":
        ws = WebSocketExecutionEngine.__new__(WebSocketExecutionEngine)
        ws._execution_engine = engine
        ws.notify_order_completed = lambda *args: None
        ws._check_and_execute_order(row.orderid, Decimal(str(price)))
    else:
        engine._fetch_quotes_batch = lambda symbols: {(row.symbol, row.exchange): quote}
        engine.check_and_execute_pending_orders()
    db_session.refresh(row)


@pytest.mark.parametrize("path", ["polling", "websocket"])
@pytest.mark.parametrize("action,activation,rebound", [
    ("SELL", 146.75, 147.45), ("BUY", 147.45, 146.75),
])
def test_gap_then_rebound_executes_activated_limit(engine, path, action, activation, rebound):
    row = order(action)
    deliver(engine, row, activation, path)
    assert row.order_status == "open" and row.filled_quantity == 0
    assert row.price_type == "SL" and row.trigger_price == Decimal("147.05")
    # Reload from persistent storage, simulating restart of the evaluating engine.
    oid = row.orderid
    db_session.remove()
    row = SandboxOrders.query.filter_by(orderid=oid).one()
    deliver(engine, row, rebound, "websocket" if path == "polling" else "polling")
    assert row.order_status == "complete" and row.filled_quantity == 1
    assert SandboxTrades.query.filter_by(orderid=oid).count() == 1
    deliver(engine, row, rebound, path)
    assert SandboxTrades.query.filter_by(orderid=oid).count() == 1


@pytest.mark.parametrize("action,price", [("SELL", 150), ("BUY", 140)])
def test_limit_match_before_trigger_cannot_fill(engine, action, price):
    row = order(action)
    deliver(engine, row, price, "polling")
    assert row.order_status == "trigger pending"
    assert SandboxTrades.query.filter_by(orderid=row.orderid).count() == 0


@pytest.mark.parametrize("action,price", [("SELL", 147), ("BUY", 147.1)])
def test_trigger_and_limit_match_on_same_tick(engine, action, price):
    row = order(action)
    deliver(engine, row, price, "websocket")
    assert row.order_status == "complete"
    assert SandboxTrades.query.filter_by(orderid=row.orderid).count() == 1


def test_activated_stop_stays_open_below_limit_and_survives_modification(engine):
    row = order()
    deliver(engine, row, 146.75, "polling")
    row.price = Decimal("146.90")
    row.trigger_price = Decimal("147.10")
    db_session.commit()
    deliver(engine, row, 146.8, "websocket")
    assert row.order_status == "open" and row.filled_quantity == 0
    deliver(engine, row, 148, "polling")
    assert row.order_status == "complete"


@pytest.mark.parametrize("status", ["cancelled", "rejected", "complete"])
def test_terminal_order_never_executes(engine, status):
    row = order(status=status)
    engine._process_order(row, {"ltp": 147, "bid": 147, "ask": 147})
    assert row.order_status == status
    assert SandboxTrades.query.filter_by(orderid=row.orderid).count() == 0


@pytest.mark.parametrize("quote", [{}, {"ltp": 0}, {"ltp": None},
                                   {"ltp": 146.75, "low": 160, "high": 170}])
def test_unusable_quote_cannot_activate_or_fill(engine, quote):
    row = order()
    engine._process_order(row, quote)
    db_session.refresh(row)
    assert row.order_status == "trigger pending"
    assert SandboxTrades.query.filter_by(orderid=row.orderid).count() == 0


@pytest.mark.parametrize("action,price", [("SELL", 150), ("BUY", 140)])
def test_already_activated_market_stop_does_not_recheck_trigger(engine, action, price):
    row = order(action, status="open", kind="SL-M")
    deliver(engine, row, price, "polling")
    assert row.order_status == "complete"


def test_placement_activated_limit_can_fill_after_quote_deferred(engine):
    row = order(status="open")
    engine._process_order(row, {"ltp": 147.45, "low": 160, "high": 170})
    assert row.order_status == "open"
    deliver(engine, row, 147.45, "websocket")
    assert row.order_status == "complete"


def test_cancel_winner_prevents_a_stale_fill(engine):
    row = order(status="open")
    SandboxOrders.query.filter_by(orderid=row.orderid).update({"order_status": "cancelled"}, synchronize_session=False)
    db_session.commit()
    # The caller still holds the stale pending object; database state must fence it.
    engine._execute_order(row, Decimal("147.45"))
    db_session.refresh(row)
    assert row.order_status == "cancelled"
    assert SandboxTrades.query.filter_by(orderid=row.orderid).count() == 0


def test_websocket_and_polling_fill_claims_create_one_trade(engine):
    row = order(status="open")
    oid = row.orderid
    barrier = Barrier(2)

    def attempt():
        try:
            candidate = SandboxOrders.query.filter_by(orderid=oid).one()
            barrier.wait(timeout=5)
            engine._execute_order(candidate, Decimal("147.45"))
        finally:
            db_session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    db_session.expire_all()
    assert SandboxTrades.query.filter_by(orderid=oid).count() == 1
    assert SandboxOrders.query.filter_by(orderid=oid).one().filled_quantity == 1


def test_fill_winner_cannot_be_overwritten_by_cancellation(engine):
    from sandbox.order_manager import OrderManager
    row = order(status="open")
    engine._execute_order(row, Decimal("147.45"))
    released = []
    manager = OrderManager.__new__(OrderManager)
    manager.user_id = row.user_id
    manager.fund_manager = SimpleNamespace(release_margin=lambda *args: released.append(args))
    manager._publish_order_update_event = lambda *args, **kwargs: None
    success, _, code = manager.cancel_order(row.orderid)
    assert not success and code in (400, 409) and released == []
    db_session.refresh(row)
    assert row.order_status == "complete"
    assert SandboxTrades.query.filter_by(orderid=row.orderid).count() == 1
