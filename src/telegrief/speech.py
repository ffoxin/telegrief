"""Speech-to-text for the search index, through Groq's hosted Whisper."""

from __future__ import annotations

from typing import Any, Protocol

import groq

from .errors import Busy

TIMEOUT_S = 120.0
# Whisper invents text over silence and music; segments it marks as probably
# not speech are left out.
NO_SPEECH_MAX = 0.6


class Untranscribable(Exception):
    """The clip was rejected; retrying is pointless."""


class Transcriber(Protocol):
    async def transcribe(self, clip: bytes) -> str: ...


def spoken_text(segments: list[Any]) -> str:
    """Text of the segments Whisper considers speech, joined."""
    parts = [
        str(s.get("text", "")).strip()
        for s in segments
        if float(s.get("no_speech_prob", 0.0)) <= NO_SPEECH_MAX
    ]
    return " ".join(p for p in parts if p)


class GroqTranscriber:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = groq.AsyncGroq(api_key=api_key, timeout=TIMEOUT_S)
        self._model = model

    async def transcribe(self, clip: bytes) -> str:
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                file=("clip.ogg", clip, "audio/ogg"),
                response_format="verbose_json",
                temperature=0.0,
            )
        except groq.RateLimitError as exc:
            raise Busy(f"Groq: {exc.message}") from exc
        except groq.BadRequestError as exc:
            raise Untranscribable(exc.message) from exc
        segments = getattr(result, "segments", None)
        if isinstance(segments, list):
            return spoken_text(segments)
        return result.text.strip()
