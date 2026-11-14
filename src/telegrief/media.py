"""The soundtrack of a video or audio file, cut down for speech-to-text, through ffmpeg."""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

SPEECH_MAX_S = 120
FFMPEG_TIMEOUT_S = 120.0
_OGG_MAGIC = b"OggS"


async def speech_clip(media: Path, max_seconds: int = SPEECH_MAX_S) -> bytes | None:
    """First ``max_seconds`` of the sound as mono 16 kHz Opus in Ogg.

    None when the file has no sound, cannot be decoded, or ffmpeg is missing.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(media),
            "-vn",
            "-t",
            str(max_seconds),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            "-f",
            "ogg",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found; speech in videos and audio is not indexed")
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("ffmpeg timed out on {}", media.name)
        return None
    if proc.returncode != 0 or not out.startswith(_OGG_MAGIC):
        logger.debug("no sound taken from {}: {}", media.name, err.decode(errors="replace"))
        return None
    return out
