from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from broker.kotak.database import master_contract_db as master


def contract(exchange="NFO", kind="CE", token="same"):
    return {"symbol": f"NIFTY31DEC3022600{kind}", "brsymbol": f"fixture-{exchange}-{kind}",
            "name": "NIFTY", "exchange": exchange, "brexchange": exchange,
            "token": token, "expiry": "31-DEC-30", "strike": 22600,
            "lotsize": 65, "instrumenttype": kind, "tick_size": .05}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "master.db"))
    master.Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(master, "db_session", session)
    session.add(master.SymToken(**contract("NSE", token="old")))
    session.commit()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(master.socketio, "emit", lambda *_args: None)
    processors = {"NSE_CM": "process_kotak_nse_csv", "NSE_FO": "process_kotak_nfo_csv",
                  "BSE_CM": "process_kotak_bse_csv", "CDE_FO": "process_kotak_cds_csv",
                  "MCX_FO": "process_kotak_mcx_csv", "BSE_FO": "process_kotak_bfo_csv"}
    def download(path):
        names = []
        for name in processors:
            target = Path(path) / (name + ".csv")
            target.write_text("fixture")
            names.append(str(target))
        return names
    monkeypatch.setattr(master, "download_csv_kotak_data", download)
    exchanges = ["NSE", "NFO", "BSE", "CDS", "MCX", "BFO"]
    for (_segment, fn), exchange in zip(processors.items(), exchanges):
        rows = [contract(exchange)]
        if exchange == "NFO":
            rows.append(contract("NFO", "PE", token="put"))
        monkeypatch.setattr(master, fn, lambda _path, rows=rows: pd.DataFrame(rows))
    yield session
    session.remove()
    engine.dispose()


def test_partial_download_does_not_erase_existing_master(isolated, monkeypatch):
    monkeypatch.setattr(master, "download_csv_kotak_data", lambda path: [str(Path(path) / "NSE_CM.csv")])
    with pytest.raises(RuntimeError, match="existing contracts retained"):
        master.master_contract_download()
    assert [r.token for r in isolated.query(master.SymToken).all()] == ["old"]


def test_processing_error_does_not_erase_existing_master(isolated, monkeypatch):
    def broken(_path):
        raise ValueError("Malformed NFO fixture")
    monkeypatch.setattr(master, "process_kotak_nfo_csv", broken)
    with pytest.raises(RuntimeError):
        master.master_contract_download()
    assert [r.token for r in isolated.query(master.SymToken).all()] == ["old"]


def test_failed_insert_rolls_back_the_deletion(isolated, monkeypatch):
    monkeypatch.setattr(isolated, "bulk_insert_mappings", lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture insert failure")))
    with pytest.raises(RuntimeError):
        master.master_contract_download()
    assert [r.token for r in isolated.query(master.SymToken).all()] == ["old"]


def test_full_refresh_is_atomic_and_keeps_cross_exchange_token_collisions(isolated):
    master.master_contract_download()
    rows = isolated.query(master.SymToken).all()
    assert len(rows) == 7
    assert {r.exchange for r in rows if r.token == "same"} == {"NSE", "NFO", "BSE", "CDS", "MCX", "BFO"}
    assert master.current_nifty_options_ready()


@pytest.mark.parametrize("change", [{"expiry": "01-JAN-20"}, {"lotsize": 0}, {"tick_size": 0}, {"tick_size": float("inf")}])
def test_readiness_requires_current_usable_call_and_put(change):
    rows = [contract(), contract(kind="PE", token="put")]
    assert master._current_nifty_options(rows, date(2026, 10, 6))
    rows[1].update(change)
    assert not master._current_nifty_options(rows, date(2026, 10, 6))


def test_cash_only_master_is_not_ready(isolated):
    assert not master.current_nifty_options_ready()


def test_cached_download_with_missing_options_forces_refresh(monkeypatch):
    import utils.auth_utils as auth
    launched = []
    monkeypatch.setattr(auth, "init_broker_status", lambda _broker: None)
    monkeypatch.setattr(auth, "should_download_master_contract", lambda _broker: (False, "cached"))
    monkeypatch.setattr(master, "current_nifty_options_ready", lambda: False)
    class Thread:
        def __init__(self, *, target, args, daemon):
            launched.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(auth, "Thread", Thread)
    auth.prepare_broker_contracts("kotak")
    assert launched == [(auth.async_master_contract_download, ("kotak",))]
