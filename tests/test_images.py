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
