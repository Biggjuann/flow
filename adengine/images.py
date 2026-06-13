"""Optional AI creative-image generation for ads.

The launcher calls a provider `(prompt) -> bytes | None` per ad at launch time
and uploads the result to Meta as a real creative image. Default is no
provider configured → ads use the link-preview image (current behaviour),
so this never blocks a launch.

Provider is selected by env:
    ADENGINE_IMAGE_PROVIDER=openai
    OPENAI_API_KEY=sk-...           (or ADENGINE_IMAGE_API_KEY)
"""
from __future__ import annotations

import base64
import os
from typing import Callable, Optional

import httpx

ImageProvider = Callable[[str], Optional[bytes]]

# Appended to every image_prompt so output is ad-appropriate.
STYLE_SUFFIX = (
    ", professional advertising photography, vibrant, high detail, "
    "clean composition, square 1:1 framing, no text, no watermark, no logo"
)

OPENAI_BASE = "https://api.openai.com/v1"


class OpenAIImageProvider:
    """Generates a square ad image via the OpenAI Images API (gpt-image-1)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-image-1",
        size: str = "1024x1024",
        client: httpx.Client | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.size = size
        self._client = client or httpx.Client(base_url=OPENAI_BASE, timeout=timeout)

    def __call__(self, prompt: str) -> bytes | None:
        response = self._client.post(
            "/images/generations",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "prompt": f"{prompt}{STYLE_SUFFIX}",
                "size": self.size,
                "n": 1,
            },
        )
        if response.status_code >= 400:
            return None
        data = (response.json().get("data") or [])
        if not data:
            return None
        item = data[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            img = self._client.get(item["url"])
            return img.content if img.status_code < 400 else None
        return None


def provider_from_env(client: httpx.Client | None = None) -> ImageProvider | None:
    kind = (os.environ.get("ADENGINE_IMAGE_PROVIDER") or "").lower()
    if kind == "openai":
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ADENGINE_IMAGE_API_KEY")
        if key:
            return OpenAIImageProvider(key, client=client)
    return None
