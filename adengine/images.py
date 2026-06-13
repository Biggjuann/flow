"""Optional AI creative-image generation for ads.

Two uses:
1. The web app generates preview images per launch-ready ad *before* launch, so
   you can review the real creative in the UI.
2. At launch, the launcher reuses those saved previews (or generates on the fly)
   and uploads them to Meta as real creative images.

Default is no provider configured -> ads use the link-preview image, so this
never blocks a launch.

Provider is selected by env:
    ADENGINE_IMAGE_PROVIDER=openai
    OPENAI_API_KEY=sk-...           (or ADENGINE_IMAGE_API_KEY)
    ADENGINE_IMAGE_MODEL=gpt-image-1   (optional; e.g. dall-e-3 needs no org verification)
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Callable, Optional

import httpx

ImageProvider = Callable[[str], Optional[bytes]]

# Appended to every image_prompt so output is ad-appropriate. The UI guard is a
# backstop: never render fabricated app interfaces/screenshots, which look fake
# (and can breach Meta policy) — show real-world subjects and outcomes instead.
STYLE_SUFFIX = (
    ", professional advertising photography, real-world scene, vibrant, high detail, "
    "clean composition, square 1:1 framing, no text, no watermark, no logo, "
    "no app user interface, no screenshots, no phone screen content, "
    "no fabricated screens, dashboards, or on-screen text"
)

OPENAI_BASE = "https://api.openai.com/v1"


class OpenAIImageProvider:
    """Generates a square ad image via the OpenAI Images API.

    Records ``last_error`` on every call so the caller can surface *why* a
    generation failed instead of silently falling back.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-image-1",
        size: str = "1024x1024",
        client: httpx.Client | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.api_key = api_key
        self.model = os.environ.get("ADENGINE_IMAGE_MODEL") or model
        self.size = size
        self.last_error: str | None = None
        self._client = client or httpx.Client(base_url=OPENAI_BASE, timeout=timeout)

    def __call__(self, prompt: str) -> bytes | None:
        self.last_error = None
        payload = {
            "model": self.model,
            "prompt": f"{prompt}{STYLE_SUFFIX}",
            "size": self.size,
            "n": 1,
        }
        try:
            response = self._client.post(
                "/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            self.last_error = f"network error: {exc}"
            return None
        if response.status_code >= 400:
            self.last_error = _openai_error(response)
            return None
        data = response.json().get("data") or []
        if not data:
            self.last_error = "OpenAI returned no image data"
            return None
        item = data[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            img = self._client.get(item["url"])
            if img.status_code < 400:
                return img.content
            self.last_error = f"could not download generated image (HTTP {img.status_code})"
            return None
        self.last_error = "OpenAI response had neither b64_json nor url"
        return None


def _openai_error(response: httpx.Response) -> str:
    try:
        msg = response.json().get("error", {}).get("message")
    except ValueError:
        msg = None
    base = msg or f"HTTP {response.status_code}"
    lowered = base.lower()
    if response.status_code == 403 and "verif" in lowered:
        return (
            base + " — gpt-image-1 requires a verified OpenAI organization. "
            "Verify at platform.openai.com/settings/organization/general, or set "
            "ADENGINE_IMAGE_MODEL=dall-e-3 (no verification needed)."
        )
    if response.status_code == 401:
        return "OpenAI rejected the API key (401) — check OPENAI_API_KEY is correct and active."
    if response.status_code in (429,) or "quota" in lowered or "billing" in lowered:
        return (
            f"{base} (HTTP {response.status_code}) — likely no OpenAI credit/quota. "
            "Add billing at platform.openai.com/account/billing."
        )
    return base


def provider_from_env(client: httpx.Client | None = None) -> ImageProvider | None:
    kind = (os.environ.get("ADENGINE_IMAGE_PROVIDER") or "").lower()
    if kind == "openai":
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ADENGINE_IMAGE_API_KEY")
        if key:
            return OpenAIImageProvider(key, client=client)
    return None


def image_path(images_dir: Path, ad_id: str) -> Path:
    return images_dir / f"{ad_id}.png"


# Magic-byte signatures for the formats Meta accepts as ad images.
def sniff_image_type(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def save_upload(images_dir: Path, ad_id: str, data: bytes) -> str:
    """Persist a user-uploaded creative image. Returns its content type.

    Raises ValueError if the bytes aren't a supported image format. Stored at
    the same path the launcher reads, so uploads ship to Meta automatically.
    """
    content_type = sniff_image_type(data)
    if content_type is None:
        raise ValueError("unsupported image format — upload a PNG, JPEG, or GIF")
    images_dir.mkdir(parents=True, exist_ok=True)
    image_path(images_dir, ad_id).write_bytes(data)
    return content_type


def generate_previews(
    images_dir: Path,
    ads: list,
    provider: ImageProvider,
    force: bool = False,
    locked: set[str] | None = None,
) -> dict:
    """Generate a preview image per ad into ``images_dir``.

    ``ads`` is a list of objects with ``.id`` and ``.creative_direction.image_prompt``
    (AdConcept). Returns a manifest dict: per-ad ok/error/source plus totals.
    Already-generated images are reused unless ``force``. Ads in ``locked``
    (user uploads) are never regenerated, even with ``force``.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    locked = locked or set()
    items: dict[str, dict] = {}
    generated = 0
    for ad in ads:
        ad_id = ad.id or ""
        prompt = ad.creative_direction.image_prompt
        path = image_path(images_dir, ad_id)

        if ad_id in locked and path.exists():
            items[ad_id] = {"ok": True, "source": "upload", "cached": True, "error": None}
            generated += 1
            continue
        if path.exists() and not force:
            items[ad_id] = {"ok": True, "source": "generated", "cached": True, "error": None}
            generated += 1
            continue
        if not prompt:
            items[ad_id] = {"ok": False, "source": "generated", "error": "ad has no image_prompt"}
            continue

        data = provider(prompt)
        if data:
            path.write_bytes(data)
            items[ad_id] = {"ok": True, "source": "generated", "cached": False, "error": None}
            generated += 1
        else:
            error = getattr(provider, "last_error", None) or "image generation failed"
            items[ad_id] = {"ok": False, "source": "generated", "error": error}

    return {
        "total": len(ads),
        "generated": generated,
        "failed": len(ads) - generated,
        "model": getattr(provider, "model", None),
        "ads": items,
    }
