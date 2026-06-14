import base64

import httpx

from adengine.images import OpenAIImageProvider, provider_from_env


def test_openai_provider_decodes_b64():
    payload = base64.b64encode(b"PNG-IMAGE-DATA").decode()
    captured = {}

    def handler(request):
        captured["body"] = request.content
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"b64_json": payload}]})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.openai.test/v1")
    provider = OpenAIImageProvider("sk-test", client=client)
    out = provider("a coffee bag on a wooden table")

    assert out == b"PNG-IMAGE-DATA"
    assert captured["auth"] == "Bearer sk-test"
    assert b"advertising photography" in captured["body"]  # style suffix appended


def test_openai_provider_handles_error():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {}})),
        base_url="https://api.openai.test/v1",
    )
    assert OpenAIImageProvider("sk", client=client)("x") is None


def test_provider_from_env(monkeypatch):
    monkeypatch.delenv("ADENGINE_IMAGE_PROVIDER", raising=False)
    assert provider_from_env() is None

    monkeypatch.setenv("ADENGINE_IMAGE_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ADENGINE_IMAGE_API_KEY", raising=False)
    assert provider_from_env() is None  # no key

    monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
    assert isinstance(provider_from_env(), OpenAIImageProvider)


# ----------------------------------------------------- preview generation

def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n-fake-png-bytes"


def test_provider_surfaces_verification_error():
    def handler(request):
        return httpx.Response(403, json={"error": {"message": "Your organization must be verified"}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.openai.test/v1")
    provider = OpenAIImageProvider("sk", client=client)
    assert provider("x") is None
    assert "verified" in provider.last_error
    assert "dall-e-3" in provider.last_error  # actionable hint


def test_generate_previews_writes_files_and_manifest(tmp_path, ads):
    from adengine.images import generate_previews

    calls = []

    def provider(prompt):
        calls.append(prompt)
        return _png()

    manifest = generate_previews(tmp_path, ads[:3], provider)
    assert manifest["generated"] == 3 and manifest["failed"] == 0
    for ad in ads[:3]:
        assert (tmp_path / f"{ad.id}.png").exists()
        assert manifest["ads"][ad.id]["ok"]
    assert len(calls) == 3


def test_generate_previews_caches_unless_force(tmp_path, ads):
    from adengine.images import generate_previews

    n = [0]

    def provider(prompt):
        n[0] += 1
        return _png()

    generate_previews(tmp_path, ads[:2], provider)
    manifest = generate_previews(tmp_path, ads[:2], provider)  # cached
    assert n[0] == 2
    assert all(v["cached"] for v in manifest["ads"].values())
    generate_previews(tmp_path, ads[:2], provider, force=True)  # regen
    assert n[0] == 4


def test_generate_previews_records_failure(tmp_path, ads):
    from adengine.images import generate_previews

    class Failing:
        model = "gpt-image-1"
        last_error = "boom: org not verified"

        def __call__(self, prompt):
            return None

    manifest = generate_previews(tmp_path, ads[:2], Failing())
    assert manifest["generated"] == 0 and manifest["failed"] == 2
    for entry in manifest["ads"].values():
        assert entry["ok"] is False and "org not verified" in entry["error"]


# ----------------------------------------------------- upload helpers

def test_sniff_image_type():
    from adengine.images import sniff_image_type
    assert sniff_image_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_image_type(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert sniff_image_type(b"GIF89a...") == "image/gif"
    assert sniff_image_type(b"not an image") is None


def test_save_upload_writes_and_rejects(tmp_path):
    from adengine.images import save_upload

    png = b"\x89PNG\r\n\x1a\n-bytes"
    assert save_upload(tmp_path, "ad_01", png) == "image/png"
    assert (tmp_path / "ad_01.png").read_bytes() == png

    import pytest
    with pytest.raises(ValueError):
        save_upload(tmp_path, "ad_02", b"<html>not an image</html>")


def test_locked_uploads_survive_regeneration(tmp_path, ads):
    from adengine.images import generate_previews, save_upload

    # ad_01 is a user upload; ad_02 is AI-generated
    save_upload(tmp_path, ads[0].id, b"\x89PNG\r\n\x1a\nUPLOAD")
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return b"\x89PNG\r\n\x1a\nAI"

    manifest = generate_previews(
        tmp_path, ads[:2], provider, force=True, locked={ads[0].id}
    )
    # locked ad never regenerated, upload bytes intact
    assert (tmp_path / f"{ads[0].id}.png").read_bytes() == b"\x89PNG\r\n\x1a\nUPLOAD"
    assert manifest["ads"][ads[0].id]["source"] == "upload"
    assert manifest["ads"][ads[1].id]["source"] == "generated"
    assert len(calls) == 1  # only ad_02 hit the provider


# ----------------------------------------------------- Meta sizing

def _real_png(w, h, color=(20, 80, 160)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _dims(data: bytes):
    import io
    from PIL import Image
    return Image.open(io.BytesIO(data)).size


def test_normalize_pads_portrait_to_square():
    from adengine.images import META_IMAGE_SIZE, normalize_for_meta
    out = normalize_for_meta(_real_png(400, 900))
    assert _dims(out) == (META_IMAGE_SIZE, META_IMAGE_SIZE)


def test_normalize_upscales_small_square():
    from adengine.images import META_IMAGE_SIZE, normalize_for_meta
    out = normalize_for_meta(_real_png(300, 300))
    assert _dims(out) == (META_IMAGE_SIZE, META_IMAGE_SIZE)


def test_normalize_passthrough_on_undecodable():
    from adengine.images import normalize_for_meta
    junk = b"\x89PNG\r\n\x1a\nnot-a-real-png"
    assert normalize_for_meta(junk) == junk  # never block a launch


def test_save_upload_sizes_to_meta(tmp_path):
    from adengine.images import META_IMAGE_SIZE, save_upload
    save_upload(tmp_path, "ad_01", _real_png(1200, 600))
    assert _dims((tmp_path / "ad_01.png").read_bytes()) == (META_IMAGE_SIZE, META_IMAGE_SIZE)


def test_generate_previews_sizes_to_meta(tmp_path, ads):
    from adengine.images import META_IMAGE_SIZE, generate_previews
    out = generate_previews(tmp_path, ads[:1], lambda p: _real_png(512, 768))
    assert out["generated"] == 1
    assert _dims((tmp_path / f"{ads[0].id}.png").read_bytes()) == (META_IMAGE_SIZE, META_IMAGE_SIZE)
