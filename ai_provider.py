"""Provider-neutral helpers for OpenAI-compatible chat-completion APIs."""

from __future__ import annotations

from typing import Any, Mapping


def chat_completions_url(base_url: str) -> str:
    """Return one normalized chat-completions endpoint."""
    return f"{(base_url or '').strip().rstrip('/')}/chat/completions"


def prepare_alibaba_payload(
    payload: Mapping[str, Any],
    *,
    model: str,
    enable_thinking: bool = False,
) -> dict[str, Any]:
    """Translate a Groq/OpenAI-style payload to Model Studio's Qwen API."""
    prepared = dict(payload)
    prepared["model"] = model

    # These controls are Groq/model-specific. Qwen uses enable_thinking.
    prepared.pop("reasoning_effort", None)
    prepared.pop("reasoning_format", None)
    prepared["enable_thinking"] = enable_thinking

    # Qwen recommends setting temperature or top_p, rather than both.
    if "temperature" in prepared:
        prepared.pop("top_p", None)
    return prepared
