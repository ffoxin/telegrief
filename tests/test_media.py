import shutil
import subprocess
from pathlib import Path

import pytest

from telegrief.media import speech_clip

TONE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=8"]
PICTURE = ["-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10"]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make(path: Path, *args: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", *args, str(path)], check=True)
    return path


def duration(clip: bytes) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", "-"],
        input=clip,
        capture_output=True,
        check=True,
    )
    return float(probe.stdout.decode().strip())


async def test_speech_clip_of_garbage_is_none(tmp_path: Path) -> None:
    path = tmp_path / "broken"
    path.write_bytes(b"not a video")
    assert await speech_clip(path) is None


@needs_ffmpeg
async def test_speech_clip_cuts_the_sound_of_a_video(tmp_path: Path) -> None:
    video = make(tmp_path / "v.mp4", *PICTURE, *TONE, "-pix_fmt", "yuv420p")
    clip = await speech_clip(video, max_seconds=3)
    assert clip is not None and clip.startswith(b"OggS")
    assert 2.5 < duration(clip) < 3.5
    whole = await speech_clip(make(tmp_path / "voice.ogg", *TONE, "-c:a", "libopus"))
    assert whole is not None and 7.5 < duration(whole) < 8.5


@needs_ffmpeg
async def test_speech_clip_of_a_silent_video_is_none(tmp_path: Path) -> None:
    video = make(tmp_path / "mute.mp4", *PICTURE, "-pix_fmt", "yuv420p")
    assert await speech_clip(video) is None
