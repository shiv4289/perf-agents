"""A tiny client for OpenAI-compatible /v1/chat/completions endpoints.

Why OpenAI-compatible? Every vLLM / NAI / NIM endpoint speaks it, with tool
calling, so the same code works for Beam, Muse Spark and Nemotron.

Stdlib only (urllib), so there is nothing to install.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .config import ModelSpec


class LLMError(RuntimeError):
    """The endpoint failed after all retries, or returned something unusable."""


def chat(spec: ModelSpec, messages: list[dict], tools: list[dict] | None = None,
         timeout_s: float = 300, retries: int = 3) -> tuple[dict, dict]:
    """Call the model once.

    Returns (assistant_message, usage):
      assistant_message  {"role": "assistant", "content": str|None, "tool_calls": [...]?}
      usage              {"prompt_tokens": int, "completion_tokens": int}
    """
    body = {
        "model": spec.model,
        "messages": messages,
        "temperature": spec.temperature,
        "max_tokens": spec.max_tokens,
        **spec.extra_body,                 # server-specific knobs from models.toml
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    headers = {"Content-Type": "application/json"}
    if spec.api_key_env:
        key = os.environ.get(spec.api_key_env)
        if not key:
            raise LLMError(f"Model {spec.name!r} needs env var {spec.api_key_env} to be set")
        headers["Authorization"] = f"Bearer {key}"

    url = spec.base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(body).encode()

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            last_err = f"HTTP {e.code} from {spec.name}: {detail}"
            if e.code < 500:              # 4xx = our request is wrong; retrying won't help
                raise LLMError(last_err) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = f"cannot reach {spec.name} at {url}: {e}"
        time.sleep(2 ** attempt)          # 1s, 2s, 4s backoff
    else:
        raise LLMError(last_err)

    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"{spec.name} returned no choices: {str(payload)[:300]}") from e

    # Keep only the fields every server accepts when we send history back.
    # (Reasoning models also return "reasoning_content"; we drop it to save context.)
    clean = {"role": "assistant", "content": msg.get("content")}
    if msg.get("tool_calls"):
        clean["tool_calls"] = [
            {"id": tc.get("id") or f"call_{i}", "type": "function",
             "function": {"name": tc["function"]["name"],
                          "arguments": tc["function"].get("arguments") or "{}"}}
            for i, tc in enumerate(msg["tool_calls"])
        ]

    usage = payload.get("usage") or {}
    return clean, {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                   "completion_tokens": int(usage.get("completion_tokens") or 0)}
