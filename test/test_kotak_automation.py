"""Offline authentication boundary fixtures: never contact the real broker."""
import hashlib
import hmac
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

from flask import Flask
import httpx
import pytest
from sqlalchemy import create_engine

from services import kotak_automation as service
from blueprints.kotak_automation import kotak_automation_bp

SECRET="s"*64
ORIGINAL_CONFIGURATION = service.configuration

@pytest.fixture
def isolated(tmp_path,monkeypatch):
    engine=create_engine("sqlite:///"+str(tmp_path/"auth.db"))
    config={"OPENALGO_AUTOMATION_SECRET":SECRET,"KOTAK_AUTOLOGIN_USERNAME":"test-user",
        "KOTAK_MOBILE_NUMBER":"+919999999999","KOTAK_MPIN":"012345","KOTAK_TOTP_SECRET":"JBSWY3DPEHPK3PXP"}
    monkeypatch.setattr(service,"configuration",lambda:config)
    import database.auth_db as auth
    monkeypatch.setattr(auth,"engine",engine)
    service.database()
    monkeypatch.setattr(auth,"get_username_by_apikey",lambda key:"test-user" if key=="api-test" else None)
    return config,engine


def signed(method,path,body=b"",nonce="a"*32,stamp=None):
    stamp=str(int(time.time())) if stamp is None else str(stamp)
    return {"Content-Type":"application/json","X-OpenAlgo-Api-Key":"api-test","X-Automation-Time":stamp,"X-Automation-Nonce":nonce,
        "X-Automation-Signature":service.signature(SECRET,method,path,stamp,nonce,body,"api-test")}


def test_signature_and_durable_replay_rejection(isolated,monkeypatch):
    monkeypatch.setattr(service,"status",lambda cfg:{"authentication":"expired","mode":"analyze","operations":[]})
    # Blueprint imports the same function by value.
    import blueprints.kotak_automation as routes
    monkeypatch.setattr(routes,"status",service.status)
    app=Flask(__name__);app.register_blueprint(kotak_automation_bp)
    client=app.test_client();path="/internal/automation/kotak/session"
    headers=signed("GET",path)
    assert client.get(path,headers=headers).status_code==200
    assert client.get(path,headers=headers).status_code==403
    assert client.get(path,headers=signed("GET",path,nonce="b"*32,stamp=time.time()-31)).status_code==403
    assert client.get(path,headers=signed("GET",path,nonce="c"*32),base_url="https://public.example").status_code==403
    forged=signed("GET",path,nonce="d"*32);forged["X-Automation-Signature"]="0"*64
    assert client.get(path,headers=forged).status_code==403


def test_ensure_rejects_credentials_in_request(isolated,monkeypatch):
    app=Flask(__name__);app.register_blueprint(kotak_automation_bp)
    client=app.test_client();path="/internal/automation/kotak/session/ensure"
    body=json.dumps({"operation_id":"1","mpin":"012345"}).encode()
    response=client.post(path,headers=signed("POST",path,body),data=body)
    assert response.status_code==400 and "012345" not in response.text


@pytest.mark.parametrize("response,expected",[(httpx.Response(200,json={"stat":"Ok"}),"valid"),
    (httpx.Response(401,json={}),"expired"),(httpx.Response(503,json={}),"unknown"),
    (httpx.Response(403,json={"emsg":"Access denied"}),"unknown"),
    (httpx.Response(200,json={"emsg":"Session expired"}),"expired")])
def test_probe_distinguishes_expiry_from_network_failure(isolated,monkeypatch,response,expected):
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):return response
    monkeypatch.setattr(service.httpx,"Client",Client)
    assert service.probe_token("token:::sid:::https://e22.kotaksecurities.com:::access")==expected
    assert service.probe_token("token:::sid:::https://other.example:::access")=="unknown"


def test_worker_reuses_valid_token_without_login(isolated,monkeypatch):
    config,engine=isolated
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"valid"})
    from sqlalchemy import insert
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="op",username="test-user",day="2026-10-05",state="running",attempts=0,updated=time.time(),details="{}"))
    app=Flask(__name__)
    service.worker(app,config,"op")
    with engine.connect() as conn:
        row=conn.execute(service.operations.select()).mappings().one()
    assert row["state"]=="completed" and row["attempts"]==0


def test_account_lock_serializes_threads_and_browser(tmp_path,monkeypatch):
    import database.auth_db as auth
    monkeypatch.setattr(auth,"engine",create_engine("sqlite:///"+str(tmp_path/"shared.db")))
    with service.authentication_lock("user"):
        with pytest.raises(RuntimeError,match="already in progress"):
            with service.authentication_lock("user"):pass
    with service.authentication_lock("user"):pass


def test_rejected_credentials_not_retried(isolated,monkeypatch):
    config,engine=isolated
    from sqlalchemy import insert
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="op",username="test-user",day="2026-10-05",state="running",attempts=0,updated=time.time(),details="{}"))
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"expired"})
    monkeypatch.setattr(service,"clock_verified",lambda:True)
    import broker.kotak.api.auth_api as auth
    calls=[]
    monkeypatch.setattr(auth,"authenticate_broker",lambda *args:(calls.append(args) or (None,"Kotak rejected MPIN authentication")))
    service.worker(Flask(__name__),config,"op")
    with engine.connect() as conn:
        row=conn.execute(service.operations.select()).mappings().one()
    assert len(calls)==1 and calls[0][2]=="012345"
    assert row["state"]=="credential_rejected"
    assert "012345" not in row["details"] and config["KOTAK_TOTP_SECRET"] not in row["details"]


def test_wrong_account_key_is_rejected(isolated):
    app=Flask(__name__);app.register_blueprint(kotak_automation_bp)
    path="/internal/automation/kotak/session"
    headers=signed("GET",path)
    headers["X-OpenAlgo-Api-Key"]="different-account"
    headers["X-Automation-Signature"]=service.signature(SECRET,"GET",path,headers["X-Automation-Time"],headers["X-Automation-Nonce"],b"","different-account")
    assert app.test_client().get(path,headers=headers).status_code==403


@pytest.mark.parametrize("seconds,expected",[(0,True),(15,False),(-15,False)])
def test_clock_check_rejects_drift(isolated,monkeypatch,seconds,expected):
    from email.utils import formatdate
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def head(self,url):return httpx.Response(405,headers={"Date":formatdate(time.time()+seconds,usegmt=True)})
    monkeypatch.setattr(service.httpx,"Client",Client)
    assert service.clock_verified() is expected


def test_clock_failure_does_not_submit_login(isolated,monkeypatch):
    config,engine=isolated
    from sqlalchemy import insert
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="op",username="test-user",day="2026-10-05",state="running",attempts=0,updated=time.time(),details="{}"))
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"expired"})
    monkeypatch.setattr(service,"clock_verified",lambda:False)
    import broker.kotak.api.auth_api as auth
    monkeypatch.setattr(auth,"authenticate_broker",lambda *args:pytest.fail("Clock failure submitted broker login"))
    service.worker(Flask(__name__),config,"op")
    with engine.connect() as conn:
        row=conn.execute(service.operations.select()).mappings().one()
    assert row["state"]=="blocked" and row["attempts"]==0


def test_source_identity_is_nonsecret_and_reproducible():
    identity = service.source_identity()
    assert identity == service.source_identity()
    assert identity["contract_version"] == 1
    assert len(identity["automation_source_digest"]) == 64


def test_successful_login_preserves_seed_boundary_and_machine_identity(isolated,monkeypatch):
    config,engine=isolated
    from sqlalchemy import insert, select
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="op",username="test-user",day="2026-10-05",state="running",attempts=0,updated=time.time(),details="{}"))
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"expired"})
    monkeypatch.setattr(service,"clock_verified",lambda:True)
    monkeypatch.setattr(service,"probe_token",lambda token:"valid")
    import broker.kotak.api.auth_api as auth_api
    import database.auth_db as auth_db
    import utils.auth_utils as auth_utils
    calls=[]
    monkeypatch.setattr(service.time,"time",lambda:1770000001.0)
    monkeypatch.setattr(service.pyotp.TOTP,"now",lambda self:self.at(1770000001))
    def login(mobile,totp,mpin):
        assert mpin=="012345"
        assert totp==service.pyotp.TOTP(config["KOTAK_TOTP_SECRET"]).at(1770000001)
        calls.append("login")
        return "fixture-token",None
    monkeypatch.setattr(auth_api,"authenticate_broker",login)
    monkeypatch.setattr(auth_db,"remove_session",lambda sid:calls.append("remove"))
    monkeypatch.setattr(auth_db,"register_session",lambda *a,**kw:calls.append("identity") or True)
    monkeypatch.setattr(auth_utils,"persist_broker_authentication",lambda *a:calls.append("persist") or True)
    service.worker(Flask(__name__),config,"op")
    assert calls==["login","remove","identity","persist"]
    with engine.connect() as conn:
        row=conn.execute(select(service.operations)).mappings().one()
        events=conn.execute(select(service.records)).mappings().all()
    assert row["state"]=="completed" and row["attempts"]==1
    assert [event["state"] for event in events]==["running","completed"]
    with pytest.raises(Exception,match="immutable"):
        with engine.begin() as conn:conn.execute(service.records.delete())


def test_rate_limits_have_three_persisted_attempts(isolated,monkeypatch):
    config,engine=isolated
    from sqlalchemy import insert,select
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="op",username="test-user",day="2026-10-05",state="running",attempts=0,updated=time.time(),details="{}"))
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"expired"})
    monkeypatch.setattr(service,"clock_verified",lambda:True)
    monkeypatch.setattr(service.time,"time",lambda:1770000001.0)
    sleeps=[]
    monkeypatch.setattr(service.time,"sleep",sleeps.append)
    import broker.kotak.api.auth_api as auth_api
    calls=[]
    monkeypatch.setattr(auth_api,"authenticate_broker",lambda *a:calls.append(a) or (None,"Kotak authentication rate limited"))
    service.worker(Flask(__name__),config,"op")
    with engine.connect() as conn:
        row=conn.execute(select(service.operations)).mappings().one()
        events=conn.execute(select(service.records)).mappings().all()
    assert len(calls)==3 and sleeps==[30,30]
    assert row["state"]=="blocked" and row["attempts"]==3
    assert len([e for e in events if e["state"]=="running"])==3


def test_expiring_cookie_does_not_revoke_during_machine_authentication(isolated,monkeypatch):
    from flask import session
    import utils.session as sessions
    import database.auth_db as auth
    from contextlib import contextmanager
    @contextmanager
    def busy(username):
        raise RuntimeError("Broker authentication already in progress")
        yield
    monkeypatch.setattr(service,"authentication_lock",busy)
    removed=[]
    monkeypatch.setattr(auth,"remove_session",removed.append)
    monkeypatch.setattr(sessions,"_revoke_user_tokens",lambda *a:pytest.fail("Stale cookie revoked in-flight authentication"))
    app=Flask(__name__);app.secret_key="fixture"
    with app.test_request_context():
        session.update(user="test-user",broker="kotak",session_id="stale-browser")
        sessions.revoke_user_tokens()
    assert removed==["stale-browser"]


def test_configuration_preserves_mpin_and_rejects_wrong_seed(isolated,monkeypatch):
    config,_=isolated
    for name,value in config.items():monkeypatch.setenv(name,value)
    monkeypatch.setenv("KOTAK_AUTOLOGIN_ENABLED","true")
    import database.user_db as users
    import utils.config as credentials
    monkeypatch.setattr(users,"find_user_by_exact_username",lambda name:object())
    monkeypatch.setattr(credentials,"get_broker_api_key",lambda:"fixture-ucc")
    monkeypatch.setattr(credentials,"get_broker_api_secret",lambda:"fixture-access")
    assert ORIGINAL_CONFIGURATION()["KOTAK_MPIN"]=="012345"
    # Standard unpadded authenticator seeds must validate exactly as PyOTP does.
    import base64
    for raw in (b"a", b"ab", b"abc", b"abcd", b"abcde", b"sixteen-byte-key"):
        padded = base64.b32encode(raw).decode()
        for seed in (padded, padded.rstrip("="), padded.rstrip("=").lower()):
            monkeypatch.setenv("KOTAK_TOTP_SECRET", seed)
            assert ORIGINAL_CONFIGURATION()["KOTAK_TOTP_SECRET"] == seed
            assert service.pyotp.TOTP(seed).at(1770000000) == service.pyotp.TOTP(padded).at(1770000000)
    monkeypatch.setenv("KOTAK_TOTP_SECRET","invalid-secret-seed")
    with pytest.raises(ValueError,match="seed format"):ORIGINAL_CONFIGURATION()
    monkeypatch.setenv("KOTAK_AUTOLOGIN_ENABLED","false")
    with pytest.raises(ValueError,match="disabled"):ORIGINAL_CONFIGURATION()


def test_container_environment_wins_at_both_dotenv_loading_boundaries():
    import ast
    from pathlib import Path
    root=Path(service.__file__).resolve().parents[1]
    for name in ("utils/config.py","utils/env_check.py"):
        tree=ast.parse((root/name).read_text())
        loads=[node for node in ast.walk(tree) if isinstance(node,ast.Call)
               and isinstance(node.func,ast.Name) and node.func.id=="load_dotenv"]
        assert loads
        assert all(any(k.arg=="override" and isinstance(k.value,ast.Constant) and k.value.value is False
                       for k in call.keywords) for call in loads)


def test_valid_token_with_stale_contracts_refreshes_without_login(isolated,monkeypatch):
    config,engine = isolated
    from sqlalchemy import insert, select
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="contracts",username="test-user",day="2026-10-06",
            state="running",attempts=0,updated=time.time(),details="{}"))
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"valid","contracts_ready":False})
    import utils.auth_utils as auth_utils
    import broker.kotak.api.auth_api as auth_api
    refreshed=[]
    monkeypatch.setattr(auth_utils,"prepare_broker_contracts",refreshed.append)
    monkeypatch.setattr(auth_api,"authenticate_broker",lambda *a:pytest.fail("Valid login was renewed"))
    service.worker(Flask(__name__),config,"contracts")
    with engine.connect() as conn:row=conn.execute(select(service.operations)).mappings().one()
    assert refreshed==["kotak"] and row["state"]=="completed" and row["attempts"]==0


def test_contract_operation_never_renews_an_expired_token(isolated,monkeypatch):
    config,engine=isolated
    from sqlalchemy import insert,select
    with engine.begin() as conn:
        conn.execute(insert(service.operations).values(id="contracts:fixture",username="test-user",day="2026-10-06",
            state="running",attempts=0,updated=time.time(),details="{}"))
    monkeypatch.setattr(service,"authentication_lock",lambda user:nullcontext())
    monkeypatch.setattr(service,"status",lambda cfg:{"mode":"analyze","authentication":"expired"})
    import broker.kotak.api.auth_api as auth_api
    monkeypatch.setattr(auth_api,"authenticate_broker",lambda *a:pytest.fail("Contract preparation renewed authentication"))
    service.worker(Flask(__name__),config,"contracts:fixture")
    with engine.connect() as conn:row=conn.execute(select(service.operations)).mappings().one()
    assert row["state"]=="blocked" and row["attempts"]==0
