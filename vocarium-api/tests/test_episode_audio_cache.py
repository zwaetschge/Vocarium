from pathlib import Path
import subprocess
import pytest
from hoerspiele import engine as e

@pytest.mark.parametrize('extract', [e.extract_episode_audio, e.extract_episode_delivery_audio])
def test_timeout_cannot_publish_partial_audio(tmp_path, monkeypatch, extract):
    output=tmp_path/'episode.flac'
    monkeypatch.setattr(e, 'local_plex_media_path', lambda _: tmp_path/'source.mkv')
    monkeypatch.setattr(e.shutil, 'which', lambda _: '/usr/bin/ffmpeg')
    def fail(args, **kwargs):
        Path(args[-1]).write_bytes(b'x'*2000)
        assert kwargs['timeout']==1800
        raise subprocess.TimeoutExpired(args, 1800)
    monkeypatch.setattr(e.subprocess, 'run', fail)
    with pytest.raises(subprocess.TimeoutExpired): extract({'duration_ms':10000},output)
    assert not output.exists()
    assert not list(tmp_path.glob('*.pending*'))

@pytest.mark.parametrize('old_duration', [0, 3000, None])
def test_incomplete_legacy_cache_is_replaced_only_after_success(tmp_path, monkeypatch, old_duration):
    output=tmp_path/'episode.flac';output.write_bytes(b'old'*1000)
    monkeypatch.setattr(e, 'local_plex_media_path', lambda _: tmp_path/'source.mkv')
    monkeypatch.setattr(e.shutil, 'which', lambda _: '/usr/bin/ffmpeg')
    def duration(path):
        if path==output:
            if old_duration is None: raise ValueError('unknown duration')
            return old_duration
        return 10000
    monkeypatch.setattr(e,'media_duration_ms',duration)
    def success(args, **kwargs):
        assert output.read_bytes()==b'old'*1000
        assert Path(args[-1])!=output
        Path(args[-1]).write_bytes(b'new'*1000)
    monkeypatch.setattr(e.subprocess,'run',success)
    assert e.extract_episode_audio({'duration_ms':10000},output)==output
    assert output.read_bytes()==b'new'*1000

def test_complete_cache_is_reused(tmp_path,monkeypatch):
    output=tmp_path/'episode.flac';output.write_bytes(b'x'*2000)
    monkeypatch.setattr(e,'local_plex_media_path',lambda _:tmp_path/'source.mkv')
    monkeypatch.setattr(e,'media_duration_ms',lambda _:10000)
    monkeypatch.setattr(e.subprocess,'run',lambda *a,**kw:pytest.fail('Cache triggered extraction'))
    assert e.extract_episode_audio({'duration_ms':10000},output)==output
