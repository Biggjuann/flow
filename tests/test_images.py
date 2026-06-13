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
