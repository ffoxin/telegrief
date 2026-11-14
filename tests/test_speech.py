from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest

from telegrief.errors import Busy
from telegrief.speech import GroqTranscriber, Untranscribable, spoken_text


class FakeTranscriptions:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def transcriber(result: Any) -> tuple[GroqTranscriber, FakeTranscriptions]:
    t = GroqTranscriber("key", "whisper-large-v3")
    fake = FakeTranscriptions(result)
    t._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=fake))  # type: ignore[assignment]
    return t, fake


def error(cls: type[groq.APIStatusError], status: int) -> groq.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.groq.com"))
    return cls("nope", response=response, body=None)


def test_spoken_text_drops_segments_that_are_probably_not_speech() -> None:
    segments = [
        {"text": " Всем привет,", "no_speech_prob": 0.01},
        {"text": " Субтитры сделал DimaTorzok", "no_speech_prob": 0.93},
        {"text": " сегодня про ёлку. ", "no_speech_prob": 0.2},
        {"text": "  "},
    ]
    assert spoken_text(segments) == "Всем привет, сегодня про ёлку."


async def test_transcribe_sends_the_clip_and_keeps_only_speech() -> None:
    result = SimpleNamespace(
        text="Привет. Продолжение следует...",
        segments=[
            {"text": " Привет.", "no_speech_prob": 0.1},
            {"text": " Продолжение следует...", "no_speech_prob": 0.9},
        ],
    )
    t, fake = transcriber(result)
    assert await t.transcribe(b"OggS...") == "Привет."
    (request,) = fake.requests
    assert request["model"] == "whisper-large-v3"
    assert request["file"] == ("clip.ogg", b"OggS...", "audio/ogg")
    assert request["response_format"] == "verbose_json"


async def test_transcribe_without_segments_uses_the_text() -> None:
    t, _ = transcriber(SimpleNamespace(text=" Привет. "))
    assert await t.transcribe(b"x") == "Привет."


async def test_rejected_clip_is_permanent_rate_limit_is_busy() -> None:
    t, _ = transcriber(error(groq.BadRequestError, 400))
    with pytest.raises(Untranscribable):
        await t.transcribe(b"x")
    t, _ = transcriber(error(groq.RateLimitError, 429))
    with pytest.raises(Busy):
        await t.transcribe(b"x")
    t, _ = transcriber(error(groq.InternalServerError, 500))
    with pytest.raises(groq.InternalServerError):
        await t.transcribe(b"x")
