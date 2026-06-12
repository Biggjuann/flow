"""Thin Anthropic client wrapper.

Structured output helper: every call forces a tool whose ``input_schema`` is
the Pydantic model's JSON schema, then validates the tool input against the
model. Freeform text is never parsed. On validation failure the call is
retried with the validation errors fed back to the model.

All calls are logged (model, tokens, latency) as JSONL to the run directory.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "claude-sonnet-4-5"

# Model families that reject sampling params (temperature/top_p/top_k).
_NO_SAMPLING_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable",
    "claude-mythos",
)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def resolve_model() -> str:
    return (
        os.environ.get("ADENGINE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or DEFAULT_MODEL
    )


class LLMClient:
    def __init__(
        self,
        model: str | None = None,
        client: Any | None = None,
        log_path: str | Path | None = None,
        max_validation_retries: int = 2,
    ) -> None:
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set. Export it locally (or add it to "
                    "your Railway service variables) and retry."
                )
            import anthropic

            client = anthropic.Anthropic(max_retries=3)
        self._client = client
        self.model = model or resolve_model()
        self.log_path = Path(log_path) if log_path else None
        self.max_validation_retries = max_validation_retries
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def structured(
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        name: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 4096,
    ) -> T:
        """Call the model and return a validated ``schema`` instance."""
        call_name = name or schema.__name__
        tool_name = f"record_{schema.__name__.lower()}"
        tool = {
            "name": tool_name,
            "description": f"Record the final {schema.__name__} result.",
            "input_schema": schema.model_json_schema(),
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_error: str | None = None

        for attempt in range(1, self.max_validation_retries + 2):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": tool_name},
            }
            if temperature is not None and not self.model.startswith(_NO_SAMPLING_PREFIXES):
                kwargs["temperature"] = temperature

            start = time.monotonic()
            response = self._client.messages.create(**kwargs)
            self._log(call_name, response, time.monotonic() - start, attempt)

            block = next(
                (b for b in response.content if getattr(b, "type", None) == "tool_use"),
                None,
            )
            if block is None:
                last_error = (
                    f"model returned no tool_use block "
                    f"(stop_reason={getattr(response, 'stop_reason', None)})"
                )
            else:
                try:
                    return schema.model_validate(block.input)
                except ValidationError as exc:
                    last_error = str(exc)

            messages = [
                {"role": "user", "content": user},
                {
                    "role": "user",
                    "content": (
                        "Your previous attempt was rejected by schema validation:\n"
                        f"{last_error}\n\n"
                        f"Call the {tool_name} tool again with a corrected payload. "
                        "Respect every field constraint, especially character limits."
                    ),
                },
            ]

        raise LLMError(
            f"{call_name}: invalid output after {self.max_validation_retries + 1} "
            f"attempts: {last_error}"
        )

    # ----------------------------------------------------------------- logging

    def _log(self, name: str, response: Any, latency_s: float, attempt: int) -> None:
        if self.log_path is None:
            return
        usage = getattr(response, "usage", None)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "call": name,
            "model": getattr(response, "model", self.model),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "latency_ms": round(latency_s * 1000),
            "attempt": attempt,
            "stop_reason": getattr(response, "stop_reason", None),
        }
        with self._log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
