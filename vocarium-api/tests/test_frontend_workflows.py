"""CPU-only integration checks for the frontend's persistence contracts."""
import copy
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
from library import create_library_router
from audiobooks.routes import create_audiobooks_router
from podcast.routes import _bind_episode_cast, _script_revision, _require_idle

@pytest.fixture
def workspace(tmp_path):
    database.init_db(tmp_path / 'test.db')
    db = database.get_db()
    db.execute("INSERT INTO users (id, username) VALUES (1,'one'),(2,'two')")
    db.execute("INSERT INTO ab_books (id,user_id,title,author,format,total_chapters) VALUES ('abcdef12',1,'Book','Author','txt',2)")
    db.execute("CREATE TABLE hs_projects (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL)")
    db.execute("INSERT INTO hs_projects VALUES ('project-one',1),('project-two',2)")
    db.commit()
    root = tmp_path / 'audiobooks' / 'abcdef12'
    root.mkdir(parents=True)
    (root / 'chapters.json').write_text(json.dumps([{'index':0,'title':'First'},{'index':1,'title':'Last'}]))
    (root / 'segments.json').write_text(json.dumps([{'index':0,'chapterIndex':0,'text':'First'}, {'index':1,'chapterIndex':1,'text':'Last'}]))
    current_user = lambda request: {'id':int(request.headers.get('x-test-user','1'))}
    app = FastAPI()
    app.include_router(create_library_router(get_current_user=current_user, db_getter=lambda:db))
    app.include_router(create_audiobooks_router(get_current_user=current_user, db_getter=lambda:db, tts_bridge=SimpleNamespace(), data_dir=tmp_path))
    with TestClient(app) as client:
        yield client, db
    db.close()


def test_explicit_links_are_persisted_and_user_scoped(workspace):
    client, db = workspace
    link={'book_id':'abcdef12','project_id':'project-one'}
    assert client.post('/api/library/links',json=link).status_code == 200
    assert client.post('/api/library/links',json=link).status_code == 200
    assert client.get('/api/library/links').json() == {'links':[link]}
    assert client.get('/api/library/links',headers={'x-test-user':'2'}).json() == {'links':[]}
    assert client.post('/api/library/links',json={**link,'project_id':'project-two'}).status_code == 404
    assert client.post('/api/library/links',json=link,headers={'x-test-user':'2'}).status_code == 404
    client.request('DELETE','/api/library/links',json=link,headers={'x-test-user':'2'})
    assert len(client.get('/api/library/links').json()['links']) == 1
    assert db.execute('SELECT COUNT(*) FROM gpu_queue_jobs').fetchone()[0] == 0
    client.request('DELETE','/api/library/links',json=link)
    assert client.get('/api/library/links').json() == {'links':[]}


def test_progress_only_completes_after_last_segment_and_returns_on_detail(workspace):
    client, _ = workspace
    path='/api/audiobooks/abcdef12'
    assert client.post(path+'/progress',json={'chapterIndex':0,'segmentIndex':0,'completed':True}).status_code == 200
    assert client.get(path).json()['progress']['completed'] is False
    assert client.post(path+'/progress',json={'chapterIndex':1,'segmentIndex':1}).status_code == 200
    progress = client.get(path).json()['progress']
    assert progress['chapterIndex'] == 1 and progress['segmentIndex'] == 1 and not progress['completed']
    assert client.post(path+'/progress',json={'chapterIndex':1,'segmentIndex':1,'completed':True}).status_code == 200
    assert client.get('/api/audiobooks').json()['books'][0]['progress']['completed'] is True
    assert client.post(path+'/progress',json={'chapterIndex':1,'segmentIndex':100,'completed':True}).status_code == 400
    assert client.get(path,headers={'x-test-user':'2'}).status_code == 404


def test_revision_ignores_timestamps_but_detects_text_and_voice():
    hosts=[{'id':'host','name':'Mira','voice_id':'voice1'}]
    script={'segments':[{'id':'seg','speaker':'Mira','type':'speech','text':'Hello'}]}
    script=_bind_episode_cast(script,hosts)
    assert script['segments'][0]['speaker_id'] == 'host'
    original=_script_revision(script,hosts)
    script['segments'][0]['updated_at']='tomorrow'
    assert _script_revision(script,hosts) == original
    script['segments'][0]['text']='Changed'
    assert _script_revision(script,hosts) != original
    hosts[0]['name']='Renamed'
    hosts[0]['voice_id']='voice2'
    bound=_bind_episode_cast(script,hosts,refresh_voices=True)
    assert bound['segments'][0]['speaker']=='Renamed'
    assert bound['segments'][0]['voice']=='voice2'


def test_ambiguous_legacy_speakers_are_not_silently_reassigned():
    script={'segments':[{'speaker':'Same','text':'hello'}]}
    before=copy.deepcopy(script)
    assert _bind_episode_cast(script,[{'id':'1','name':'Same'},{'id':'2','name':'Same'}]) == before


def test_active_job_refuses_editor_and_generation_mutations():
    for status in ('generating_script','generating_audio'):
        with pytest.raises(Exception) as error:
            _require_idle({'status':status})
        assert error.value.status_code == 409
    _require_idle({'status':'script_ready'})


def test_horspiel_save_preserves_audio_and_does_not_start_render(monkeypatch):
    from hoerspiele import engine
    project={'id':'p','cues':[{'id':'cue','text':'before'}],'artifacts':[{'id':'audio'}],'timeline':{'revision':1},'semantic_cues':[]}
    monkeypatch.setattr(engine,'project_or_404',lambda *args:project)
    monkeypatch.setattr(engine,'active_project_run',lambda *args:None)
    monkeypatch.setattr(engine,'save_state',lambda:None)
    monkeypatch.setattr(engine,'invalidate_automatic_quality',lambda p:None)
    monkeypatch.setattr(engine.threading,'Thread',lambda *a,**kw:pytest.fail('Editing must not start production'))
    engine.update_cue('p','cue',engine.CuePatch(text='after'),SimpleNamespace())
    assert project['cues'][0]['text']=='after'
    assert project['artifacts']==[{'id':'audio'}] and project['audio_stale']
    monkeypatch.setattr(engine,'active_project_run',lambda *args:{'status':'running'})
    with pytest.raises(Exception) as error:
        engine.update_cue('p','cue',engine.CuePatch(text='forbidden'),SimpleNamespace())
    assert error.value.status_code==409 and project['cues'][0]['text']=='after'

def test_podcast_edit_render_revision_and_old_audio_stream(workspace, tmp_path, monkeypatch):
    from podcast.routes import create_podcast_router
    import hashlib
    client, db = workspace
    audio_root = tmp_path / 'podcast_audio'
    monkeypatch.setenv('PODCAST_AUDIO_PATH', str(audio_root))
    router, assembler = create_podcast_router(get_current_user=lambda request:{'id':int(request.headers.get('x-test-user','1'))}, tts_url='http://unused', db_getter=lambda:db, gpu_submit=lambda *a,**kw:pytest.fail('No GPU work in this integration test'), assembler_output_dir=audio_root)
    async def voices(**kwargs): return {'omni-1':'omnivoice','omni-2':'omnivoice'}
    monkeypatch.setattr(assembler.tts,'engine_voices',voices)
    client.app.include_router(router)
    db.execute("INSERT INTO hosts (id,user_id,name,voice_id,role) VALUES ('host',1,'Mira','omni-1','host')")
    db.commit()
    created=client.post('/api/podcasts',json={'topic':'Test','format':'monolog','host_ids':['host']})
    assert created.status_code==200,created.text
    pod=created.json()['id']; base=f'/api/podcasts/{pod}'
    script={'segments':[{'id':'seg','speaker':'Mira','text':'Before','type':'speech','position':0,'word_count':1}]}
    directory=audio_root/pod;directory.mkdir()
    old=directory/'audio.wav';old.write_bytes(b'old waveform')
    old_sha=hashlib.sha256(old.read_bytes()).hexdigest()
    db.execute("UPDATE podcasts SET script_json=?,status='ready',audio_path=?,audio_format='wav',audio_size=?,audio_sha256=? WHERE id=? AND user_id=?", (json.dumps(script),str(old),old.stat().st_size,old_sha,pod,1));db.commit()
    detail=client.get(base).json();initial=detail['script_revision']
    assert detail['script']['segments'][0]['speaker_id']=='host'
    db.execute('UPDATE podcasts SET audio_revision=? WHERE id=? AND user_id=?',(initial,pod,1));db.commit()
    edited=client.patch(base+'/script/segments/seg',json={'text':'After','speaker_id':'host'})
    assert edited.status_code==200,edited.text
    assert edited.json()['audio_stale']
    assert client.get(base+'/audio/stream').content==b'old waveform'
    observed=[]
    async def assemble(**kwargs):
        observed.extend(kwargs['segments'])
        # The previous artifact is already archived before the assembler writes.
        assert (await asyncio.to_thread(client.get, base+'/audio/stream')).content==b'old waveform'
        old.write_bytes(b'new waveform')
        return SimpleNamespace(file_path=old,duration=2,file_size=old.stat().st_size)
    monkeypatch.setattr(assembler,'assemble_from_segments',assemble)
    rendered=client.post(base+'/audio/generate')
    assert 'event: complete' in rendered.text,rendered.text
    assert observed[0].text=='After' and observed[0].voice=='omni-1'
    detail=client.get(base).json()
    assert not detail['audio_stale'] and detail['script_revision']==detail['audio_revision']
    assert client.get(base+'/audio/stream').content==b'new waveform'
    assert client.get(base+'/audio/stream?revision='+old_sha).content==b'old waveform'
    assert client.get(base+'/audio/stream?revision='+old_sha,headers={'x-test-user':'2'}).status_code==404
    assert client.get(base+'/audio/stream?revision=../../bad').status_code==400
    # Snapshot refresh is explicit; a global name change alone does not alter this episode.
    db.execute("UPDATE hosts SET name='Changed globally',voice_id='omni-2' WHERE id='host' AND user_id=1");db.commit()
    assert client.get(base).json()['hosts'][0]['name']=='Mira'
    updated=client.patch(base,json={'host_ids':['host']})
    assert updated.status_code==200,updated.text
    assert updated.json()['script']['segments'][0]['speaker']=='Changed globally'
    assert updated.json()['script']['segments'][0]['voice']=='omni-2'
    assert updated.json()['audio_stale']
    db.execute("UPDATE podcasts SET status='generating_audio' WHERE id=? AND user_id=1",(pod,));db.commit()
    for path,body in [(base,{'host_ids':['host']}),(base+'/script/segments/seg',{'text':'Race'})]:
        assert client.patch(path,json=body).status_code==409
    assert client.post(base+'/audio/generate').status_code==409
