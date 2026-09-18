"""A mixed scene must retain every source sample, including its audible tail."""
import array
import shutil
import wave

import pytest

from hoerspiele.engine import SAMPLE_RATE, mix_narration_over_source


def write_pcm(path, frames, value):
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        out.writeframes(array.array('h', [value]) * frames)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required for the real mixing regression')
@pytest.mark.parametrize('start,voice_frames', [(0, 155280), (120001, 155280), (500001, 96000)])
def test_overlay_preserves_complete_source_window_and_tail(tmp_path, start, voice_frames):
    source = tmp_path / 'source.wav'
    voice = tmp_path / 'voice.wav'
    mixed = tmp_path / 'mixed.wav'
    duration = 155280
    write_pcm(source, SAMPLE_RATE * 20, 1000)
    write_pcm(voice, voice_frames, 5000)
    mix_narration_over_source(source, voice, mixed, start, start + duration)
    with wave.open(str(mixed), 'rb') as audio:
        assert audio.getnframes() == duration
        audio.setpos(duration - 100)
        tail = array.array('h', audio.readframes(100))
    # Padding a truncated mix with silence would hide the lost scene audio.
    assert len(tail) == 100 and min(tail) > 0
