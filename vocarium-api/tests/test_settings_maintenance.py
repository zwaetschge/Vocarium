from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from hoerspiele import engine

@pytest.fixture
def maintenance(monkeypatch):
    monkeypatch.setattr(engine,'STATE',{'agent_settings':{},'runs':{}})
    monkeypatch.setattr(engine,'save_state',lambda:None)
    calls=[]
    def control(method,path,payload=None):
        calls.append((method,path))
        return {'catalogs':{'codex':{'models':[{'value':'new-model','label':'New'}]}}}
    monkeypatch.setattr(engine,'agent_control_request',control)
    return calls

def test_discovery_is_used_for_settings_and_validation(maintenance):
    engine.get_agent_maintenance(SimpleNamespace())
    assert 'new-model' in {m['value'] for m in engine.agent_settings_response()['model_options']['codex']}
    assert engine.normalized_agent_candidate({'provider':'codex','model':'new-model'})['model']=='new-model'
    profile={'provider':'codex','model':'new-model','reasoning_effort':'high','timeout_seconds':1200,'fallbacks':[]}
    engine.update_agent_settings(engine.AgentSettingsPatch(research=profile,scripting=profile),SimpleNamespace())
    assert engine.STATE['agent_settings']['catalogs']
    assert engine.agent_settings_response()['research']['model']=='new-model'

def test_other_users_active_run_blocks_global_update(maintenance):
    engine.STATE['runs']['other']={'owner_user_id':99,'status':'running'}
    with pytest.raises(HTTPException) as exc: engine.start_agent_maintenance('clis/codex/update',SimpleNamespace(id=1))
    assert exc.value.status_code==409
    assert maintenance==[]

def test_only_allowlisted_maintenance_is_forwarded(maintenance):
    with pytest.raises(HTTPException) as exc: engine.start_agent_maintenance('clis/anything/update',SimpleNamespace())
    assert exc.value.status_code==404
    engine.start_agent_maintenance('models/refresh',SimpleNamespace())
    assert maintenance==[('POST','/maintenance/models/refresh')]

def test_routes_require_admin(maintenance,monkeypatch):
    app=FastAPI();app.include_router(engine.router)
    def denied():raise HTTPException(403,'Admin required')
    app.dependency_overrides[engine.require_admin]=denied
    with TestClient(app) as client:
        assert client.get('/api/hoerspiele/settings/agents/maintenance').status_code==403
        assert client.post('/api/hoerspiele/settings/agents/maintenance/clis/codex/update').status_code==403
    assert maintenance==[]

def test_provider_catalog_is_user_scoped_and_does_not_change_model(monkeypatch):
    import asyncio
    asyncio.run(_provider_catalog_check(monkeypatch))


async def _provider_catalog_check(monkeypatch):
    import main
    queries=[]
    class DB:
        def execute(self,query,args):queries.append((query,args));return self
        def fetchone(self):return ('http://provider/v1','private-key') if queries[-1][1]==('mine',7) else None
    monkeypatch.setattr(main,'get_current_user',lambda req:{'id':7})
    monkeypatch.setattr(main,'get_db',DB)
    monkeypatch.setattr(main,'_normalize_http_base_url',lambda url:url)
    class Response:
        status=200
        content=SimpleNamespace(read=AsyncMock(side_effect=[b'{"data":[{"id":"new"},',b'{"id":"old"}]}',b'']))
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    calls=[]
    class Session:
        def get(self,url,**kwargs):calls.append((url,kwargs));return Response()
    monkeypatch.setattr(main,'_http_session',Session)
    result=await main.list_provider_models('mine',None)
    assert result['models']==['new','old']
    assert 'private-key' not in json.dumps(result)
    assert all('user_id=?' in q and args[1]==7 for q,args in queries)
    with pytest.raises(HTTPException) as exc:await main.list_provider_models('theirs',None)
    assert exc.value.status_code==404
    assert len(calls)==1
