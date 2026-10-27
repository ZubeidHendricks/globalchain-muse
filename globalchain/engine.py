"""Model engines: live Meta Model API (Muse Spark + SAM 3.1) or a deterministic offline mock.

Callers pass a `fallback` producing the rule-based result. The mock engine returns it
directly; the Meta engine uses it only if the Muse Spark call fails or its reply cannot be
parsed. reason_json returns (result, fallback_used) so assessments can say which happened.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from openai import APIError, AsyncOpenAI, BadRequestError

from .config import Settings

log = logging.getLogger(__name__)


# Muse Spark reasoning tasks; each can run on its own model (see Settings.verify_model / seller_model).
TASKS = ("verify", "seller")


class Engine(Protocol):
    name: str
    models: dict[str, str]  # one entry per task in TASKS, plus "segmentation"

    async def segment(self, image_data_url: str, prompt: str, frame_size: tuple[int, int]) -> str: ...

    async def reason_json(
        self, task: str, developer: str, content: list[dict[str, Any]], fallback: Callable[[], dict],
        schema: dict | None = None,
    ) -> tuple[dict, bool]: ...


def extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


class MetaEngine:
    name = "meta"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.meta_api_base,
            api_key=settings.model_api_key,
            timeout=settings.http_timeout * 2,
        )
        self.models = {
            "verify": settings.verify_model or settings.muse_model,
            "seller": settings.seller_model or settings.muse_model,
            "segmentation": settings.sam_model,
        }
        self._sam_slots = asyncio.Semaphore(6)
        self._json_mode = True

    async def segment(self, image_data_url: str, prompt: str, frame_size: tuple[int, int]) -> str:
        async with self._sam_slots:
            resp = await self.client.responses.create(
                model=self.settings.sam_model,
                input=[{
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_data_url},
                    ],
                }],
                # Boxes are all we use; one_bit keeps the mask payload small.
                metadata={"mask_encoding": "one_bit"},
            )
        return resp.output_text or ""

    async def reason_json(self, task, developer, content, fallback, schema=None):
        kwargs: dict[str, Any] = {
            "model": self.models[task],
            "messages": [
                {"role": "developer", "content": developer},
                {"role": "user", "content": content},
            ],
            "reasoning_effort": self.settings.reasoning_effort,
        }
        # The Model API documents json_schema (not json_object) for structured output.
        if self._json_mode and schema:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": schema}
        try:
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                if "response_format" not in kwargs:
                    raise
                log.warning("Structured output rejected (%s); retrying without response_format", exc)
                self._json_mode = False
                kwargs.pop("response_format")
                resp = await self.client.chat.completions.create(**kwargs)
        except APIError as exc:  # timeouts, rate limits, 5xx: degrade to rules instead of failing the call
            log.warning("Muse Spark call failed (%s); using rule-based fallback", exc)
            return fallback(), True
        parsed = extract_json(resp.choices[0].message.content or "")
        if parsed is None:
            log.warning("Muse Spark returned non-JSON output; using rule-based fallback")
            return fallback(), True
        return parsed, False


class MockEngine:
    """Offline engine for development and tests. Output is clearly labelled 'mock'."""

    name = "mock"
    models = {"verify": "mock-reasoner", "seller": "mock-reasoner", "segmentation": "mock-segmenter"}

    async def segment(self, image_data_url: str, prompt: str, frame_size: tuple[int, int]) -> str:
        # Deterministic pseudo-box per (image, prompt), emitted in SAM 3.1's grammar
        # so the real parser is exercised.
        w, h = frame_size
        seed = hashlib.sha256((image_data_url[-64:] + prompt).encode()).digest()
        bw, bh = max(8, w * (20 + seed[0] % 20) // 100), max(8, h * (20 + seed[1] % 20) // 100)
        x1, y1 = (w - bw) * seed[2] // 255, (h - bh) * seed[3] // 255
        return f"<0f>0<|box;x1={x1};y1={y1};x2={x1 + bw};y2={y1 + bh};w={w};h={h}|><|mask;x=0;y=0;data=1,1,!00000|>"

    async def reason_json(self, task, developer, content, fallback, schema=None):
        return fallback(), True


def build_engine(settings: Settings) -> Engine:
    return MockEngine() if settings.mock_mode else MetaEngine(settings)
