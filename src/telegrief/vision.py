"""Searchable text for a post's pictures, written by Claude."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Protocol

import anthropic
from anthropic.types import ImageBlockParam, TextBlockParam

from .models import Image

MAX_TOKENS = 4096
TIMEOUT_S = 120.0
SYSTEM = (
    "You write search-index entries for Telegram channel posts, so that the owner can find "
    "a post later by typing a few words. You get the pictures of one post and the post's "
    "own text, if it has any.\n"
    "Reply in plain text with two parts:\n"
    "1. Every piece of text visible in the pictures, transcribed verbatim in its original "
    "language.\n"
    "2. One or two sentences saying what the pictures show: the people, objects and scene, "
    "and the joke or the point if there is one. Prefer the plain words someone would "
    "search for.\n"
    "Write the second part in the language of the post's text or of the text in the "
    "pictures; use Russian when there is neither. No headings, no markdown, and nothing "
    "about the task itself."
)


class Undescribable(Exception):
    """The pictures will never be described (rejected or refused); retrying is pointless."""


class Describer(Protocol):
    async def describe(self, images: Sequence[Image], text: str) -> str: ...


class ClaudeDescriber:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=TIMEOUT_S)
        self._model = model

    async def describe(self, images: Sequence[Image], text: str) -> str:
        content: list[ImageBlockParam | TextBlockParam] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.standard_b64encode(image.data).decode("ascii"),
                },
            }
            for image in images
        ]
        content.append(
            {"type": "text", "text": f"Post text:\n{text}" if text else "The post has no text."}
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": "low"},
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.BadRequestError as exc:
            raise Undescribable(exc.message) from exc
        if response.stop_reason == "refusal":
            raise Undescribable("refused")
        return "\n".join(b.text for b in response.content if b.type == "text").strip()
