import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('API_KEY_PEPPER','offline-test-pepper-more-than-32-characters')
os.environ.setdefault('DATABASE_URL','sqlite:///:memory:')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from broker.kotak.api import margin_api
from broker.kotak.mapping import margin_data
from services import margin_service


def parsed(required=100, rms='OK', insufficient=0):
    return margin_data.parse_margin_response(dict(stat='Ok',stCode=200,reqdMrgn=required,rmsVldtd=rms,insufFund=insufficient))


def test_complete_aggregate_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(margin_api,'transform_margin_position',lambda p:p)
    monkeypatch.setattr(margin_api,'calculate_single_margin',lambda *a:(SimpleNamespace(status_code=200),parsed()))
    response,result=margin_api.calculate_margin_api([{'leg':1},{'leg':2}],'dummy')
    assert response.status_code==200 and result['data']['total_margin_required']==200
    c=result['data']['calculation']
    assert c['complete'] and not c['verification_supported']
    assert c['requested_count']==c['result_count']==2
    assert [r['index'] for r in c['legs']]==[0,1]
    assert c['method']=='standalone_order_sum'


@pytest.mark.parametrize('failed',[0,1,2])
@pytest.mark.parametrize('http',[200,403,500])
def test_any_leg_failure_is_error_independent_of_last_leg(monkeypatch,failed,http):
    monkeypatch.setattr(margin_api,'transform_margin_position',lambda p:p)
    calls=[]
    def single(*args):
        i=len(calls);calls.append(i)
        return SimpleNamespace(status_code=http if i==failed else 200), {'status':'error'} if i==failed else parsed()
    monkeypatch.setattr(margin_api,'calculate_single_margin',single)
    response,result=margin_api.calculate_margin_api([{'leg':i} for i in range(3)],'dummy')
    assert result['status']=='error' and response.status_code>=400
    assert 'total_margin_required' not in result.get('data',{})
    assert len(calls)==failed+1


def test_bad_transform_cannot_drop_a_leg(monkeypatch):
    monkeypatch.setattr(margin_api,'transform_margin_position',lambda p:None if p['leg']==1 else p)
    monkeypatch.setattr(margin_api,'calculate_single_margin',lambda *a:pytest.fail('No dispatch before all legs validate'))
    response,result=margin_api.calculate_margin_api([{'leg':0},{'leg':1}],'dummy')
    assert response.status_code==400 and result['status']=='error'


@pytest.mark.parametrize('amount',[None,True,-1,float('nan'),float('inf'),'bad'])
def test_invalid_amount_never_becomes_zero(amount):
    assert parsed(amount)['status']=='error'


def test_zero_is_explicit_and_rms_remains_separate():
    assert parsed(0)['data']['total_margin_required']==0
    result=parsed(100,'NOT_OK',50)
    assert result['status']=='success' and result['data']['rms_approved'] is False
    assert result['data']['insufficient_funds']==50


def test_all_failures_and_empty_aggregate_are_errors():
    for rows in [[],[{'status':'error'}],[{'status':'error'},{'status':'error'}]]:
        assert margin_data.parse_batch_margin_response(rows)['status']=='error'


def test_service_cannot_promote_http200_error(monkeypatch):
    monkeypatch.setattr(margin_service,'import_broker_module',lambda _:SimpleNamespace(calculate_margin_api=lambda *a:(SimpleNamespace(status_code=200),{'status':'error','message':'failed'})))
    monkeypatch.setattr(margin_service.executor,'submit',lambda *a:None)
    success,result,status=margin_service.calculate_margin_with_auth([{'fixture':1}],'dummy','kotak',{})
    assert not success and status==500 and result['status']=='error'


def test_api_service_preserves_standalone_capability(monkeypatch):
    monkeypatch.setattr(margin_api,'transform_margin_position',lambda p:p)
    monkeypatch.setattr(margin_api,'calculate_single_margin',lambda *a:(SimpleNamespace(status_code=200),parsed()))
    monkeypatch.setattr(margin_service,'import_broker_module',lambda _:margin_api)
    monkeypatch.setattr(margin_service.executor,'submit',lambda *a:None)
    success,result,status=margin_service.calculate_margin_with_auth([{'leg':1},{'leg':2}],'dummy','kotak',{})
    assert success and status==200
    assert result['data']['calculation']['verification_supported'] is False
    assert result['data']['calculation']['scope']=='independent_orders'


@pytest.mark.parametrize('missing',['reqdMrgn','rmsVldtd','insufFund','stCode'])
def test_missing_broker_fields_remain_unavailable(missing):
    response=dict(stat='Ok',stCode=200,reqdMrgn=100,rmsVldtd='OK',insufFund=0)
    response.pop(missing)
    assert margin_data.parse_margin_response(response)['status']=='error'
