import base64
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from telegrief.errors import Busy
from telegrief.models import Image
from telegrief.vision import ClaudeDescriber, Undescribable


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def describer(result: Any) -> tuple[ClaudeDescriber, FakeMessages]:
    d = ClaudeDescriber("key", "claude-haiku-5-5")
    messages = FakeMessages(result)
    d._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return d, messages


def reply(*texts: str, stop_reason: str = "end_turn") -> Any:
    blocks = [SimpleNamespace(type="thinking", thinking="")]
    blocks += [SimpleNamespace(type="text", text=t) for t in texts]
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


async def test_describe_sends_every_picture_and_the_post_text() -> None:
    d, messages = describer(reply("НАДПИСЬ", "Кот на диване. "))
    images = [Image(b"one", "image/jpeg"), Image(b"two", "image/png")]

    assert await d.describe(images, "Подпись") == "НАДПИСЬ\nКот на диване."

    (request,) = messages.requests
    assert request["model"] == "claude-haiku-5-5"
    content = request["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"two").decode(),
    }
    assert content[2]["text"] == "Post text:\nПодпись"


async def test_describe_without_post_text() -> None:
    d, messages = describer(reply("ok"))
    await d.describe([Image(b"one", "image/jpeg")], "")
    assert messages.requests[0]["messages"][0]["content"][-1]["text"] == "The post has no text."


async def test_refusal_and_rejected_request_are_permanent() -> None:
    d, _ = describer(reply(stop_reason="refusal"))
    with pytest.raises(Undescribable):
        await d.describe([Image(b"one", "image/jpeg")], "")

    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com"))
    d, _ = describer(anthropic.BadRequestError("bad image", response=response, body=None))
    with pytest.raises(Undescribable):
        await d.describe([Image(b"one", "image/jpeg")], "")


async def test_other_api_errors_propagate_for_a_retry() -> None:
    response = httpx2.Response(529, request=httpx2.Request("POST", "https://api.anthropic.com"))
    d, _ = describer(anthropic.InternalServerError("overloaded", response=response, body=None))
    with pytest.raises(anthropic.InternalServerError):
        await d.describe([Image(b"one", "image/jpeg")], "")


async def test_rate_limit_is_busy() -> None:
    response = httpx2.Response(429, request=httpx2.Request("POST", "https://api.anthropic.com"))
    d, _ = describer(anthropic.RateLimitError("slow down", response=response, body=None))
    with pytest.raises(Busy):
        await d.describe([Image(b"one", "image/jpeg")], "")
