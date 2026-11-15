"""Speech-to-text for the search index, through Groq's hosted Whisper."""

from __future__ import annotations

import re
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


# What Whisper writes over music and noise: lines from the subtitle files it was
# trained on. Compared with letters only, case folded.
PHANTOMS = frozenset(
    {
        "",
        "субтитрысделалdimatorzok",
        "субтитрысоздавалdimatorzok",
        "субтитрыделалdimatorzok",
        "редакторсубтитров",
        "продолжениеследует",
        "спасибозапросмотр",
        "подписывайтесьнаканал",
        "thankyou",
        "thankyouforwatching",
        "thanksforwatching",
        "pleasesubscribe",
        "subscribe",
        "suscríbete",
        "suscribete",
        "you",
        "bye",
        "музыка",
        "music",
        "applause",
        "аплодисменты",
    }
)


# The same lines glued to real speech, usually at the end: cut out wherever they are.
PHANTOM_RE = re.compile(
    r"""
    (?: субтитры\s+\w+\s+[«"']?[\w.\-]+[»"']?        # Субтитры сделал DimaTorzok
      | редактор\s+субтитров\s+(?:[А-ЯЁA-Z]\.\s*)?\S+
      | корректор\s+(?:[А-ЯЁA-Z]\.\s*)?\S+
      | продолжение\s+следует
      | спасибо\s+за\s+просмотр
      | thanks?(?:\s+you)?\s+for\s+watching
    ) [.!…]*
    | \[[^\]]*\]                                      # [музыка]
    | \([^)]*(?:music|музыка|applause)[^)]*\)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _letters(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalpha())


def spoken_text(segments: list[Any]) -> str:
    """Text of the segments Whisper considers speech, joined; known phantom lines dropped."""
    parts = [
        str(s.get("text", "")).strip()
        for s in segments
        if float(s.get("no_speech_prob", 0.0)) <= NO_SPEECH_MAX
    ]
    kept = [p for p in parts if p and _letters(p) not in PHANTOMS]
    text = " ".join(PHANTOM_RE.sub(" ", " ".join(kept)).split())
    return "" if _letters(text) in PHANTOMS else text  # "" too: nothing but symbols


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
        return spoken_text([{"text": result.text}])
