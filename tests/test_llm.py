import json

import pytest
from pydantic import BaseModel, Field

from adengine.llm import LLMClient, LLMError


class Tiny(BaseModel):
    title: str = Field(max_length=10)
    count: int


def make_client(fake_anthropic, fake_response, responses, tmp_path, model="claude-sonnet-4-5"):
    fake = fake_anthropic([fake_response(r) if isinstance(r, dict) or r is None else r for r in responses])
    client = LLMClient(model=model, client=fake, log_path=tmp_path / "llm_log.jsonl")
    return client, fake


def test_structured_success_and_logging(fake_anthropic, fake_response, tmp_path):
    client, fake = make_client(
        fake_anthropic, fake_response, [{"title": "ok", "count": 3}], tmp_path
    )
    result = client.structured(Tiny, system="sys", user="usr", name="tiny_call")
    assert result == Tiny(title="ok", count=3)

    call = fake.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_tiny"}
    assert call["tools"][0]["input_schema"] == Tiny.model_json_schema()
    assert call["system"] == "sys"

    lines = (tmp_path / "llm_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["call"] == "tiny_call"
    assert record["input_tokens"] == 100
    assert record["attempt"] == 1
    assert "latency_ms" in record


def test_validation_retry_feeds_errors_back(fake_anthropic, fake_response, tmp_path):
    client, fake = make_client(
        fake_anthropic,
        fake_response,
        [{"title": "way too long title", "count": 1}, {"title": "short", "count": 1}],
        tmp_path,
    )
    result = client.structured(Tiny, system="sys", user="usr")
    assert result.title == "short"
    assert len(fake.messages.calls) == 2
    retry_messages = fake.messages.calls[1]["messages"]
    assert "rejected by schema validation" in retry_messages[-1]["content"]


def test_validation_exhaustion_raises(fake_anthropic, fake_response, tmp_path):
    bad = {"title": "x" * 50, "count": 1}
    client, _ = make_client(fake_anthropic, fake_response, [bad, bad, bad], tmp_path)
    with pytest.raises(LLMError, match="invalid output after 3 attempts"):
        client.structured(Tiny, system="sys", user="usr")


def test_temperature_dropped_on_no_sampling_models(fake_anthropic, fake_response, tmp_path):
    client, fake = make_client(
        fake_anthropic, fake_response, [{"title": "ok", "count": 1}], tmp_path,
        model="claude-opus-4-8",
    )
    client.structured(Tiny, system="s", user="u", temperature=1.0)
    assert "temperature" not in fake.messages.calls[0]

    client, fake = make_client(
        fake_anthropic, fake_response, [{"title": "ok", "count": 1}], tmp_path,
        model="claude-sonnet-4-5",
    )
    client.structured(Tiny, system="s", user="u", temperature=1.0)
    assert fake.messages.calls[0]["temperature"] == 1.0


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        LLMClient()
