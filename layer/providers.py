"""The model backend. ONE adapter, not five.

LiteLLM, OpenAI, OpenRouter, Ollama, llama.cpp and vLLM all speak the OpenAI
chat-completions shape, so base_url + key + model name covers every one of
them. There is exactly one other code path: `fixture`, which replays a canned
response so the demo works with no backend at all.

Swapping where the answer comes from moves nothing else in the layer. That is
not a convenience, it is the thesis.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "responses"


class ProviderUnreachable(RuntimeError):
    """The backend did not answer, or answered with an error.

    Its own type, because "the model is down" is a different thing from "the
    layer is broken" and the two should not read the same to whoever is looking.
    """


@dataclass
class Completion:
    text: str
    tool_call: dict | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    backend: str
    model_used: str = ""
    reasoning: str = ""


def _fixture(scenario: str, model: str) -> Completion:
    # A scenario with no file of its own falls back to the happy answer, which
    # is right for every scenario whose interest is in what the LAYER decided
    # rather than in what the model said.
    path = FIXTURES / f"{scenario}.json"
    if not path.is_file():
        path = FIXTURES / "happy.json"
    raw = json.loads(path.read_text())
    return Completion(
        text=raw.get("text", ""),
        tool_call=raw.get("tool_call"),
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
        cost_usd=raw.get("cost_usd", 0.0),
        backend="fixture",
        model_used=model,
    )


# A tool call the model is expected to want. Local models are inconsistent at
# emitting OpenAI tool_calls, and the permissions component is about the
# DECISION, not about tool-call parsing, so the scenario decides what is
# attempted and the policy decides whether it may happen.
_SCENARIO_TOOL = {
    "happy": {"id": "call_1", "name": "post_review_comment",
              "arguments": {"pr": 412, "body": "2 findings on refund.py"}},
    "secret-in-context": {"id": "call_1", "name": "post_review_comment",
                          "arguments": {"pr": 412, "body": "2 findings on refund.py"}},
    "out-of-scope-write": {"id": "call_1", "name": "patch_deployment",
                           "arguments": {"namespace": "demo-prod", "name": "checkout",
                                         "replicas": 0}},
}


def complete(model: str, prompt: str, *, scenario: str = "happy",
             backend: str | None = None) -> Completion:
    c = cfg.load()
    want = backend or os.environ.get("PROMPT_LAYER_BACKEND") or (
        "live" if c.provider.configured else "fixture")

    if want == "fixture" or not c.provider.configured:
        return _fixture(scenario, model)

    import httpx

    resolved = c.resolve_model(model)
    headers = {"Content-Type": "application/json"}
    if c.provider.api_key:
        headers["Authorization"] = f"Bearer {c.provider.api_key}"

    body: dict = {
        "model": resolved,
        "messages": [{"role": "user", "content": prompt}],
        # Enough for three findings and a sentence. Raise it if your answers
        # come back truncated; the only cost is a longer wait.
        "max_tokens": 2500 if c.provider.thinking else 600,
    }
    if not c.provider.thinking:
        # llama.cpp and vLLM honour this; backends that don't, ignore it.
        body["chat_template_kwargs"] = {"enable_thinking": False}

    url = f"{c.provider.base_url.rstrip('/')}/chat/completions"
    try:
        resp = httpx.post(url, headers=headers, json=body,
                          timeout=c.provider.timeout)
        resp.raise_for_status()
        d = resp.json()
    except httpx.HTTPStatusError as e:
        raise ProviderUnreachable(
            f"{c.provider.base_url} answered {e.response.status_code}. "
            f"Check the model name, the key, and that the path ends in /v1."
        ) from e
    except httpx.TimeoutException as e:
        raise ProviderUnreachable(
            f"{c.provider.base_url} did not answer within "
            f"{c.provider.timeout:.0f}s. Raise provider.timeout in config.yaml, "
            f"or set provider.thinking: false."
        ) from e
    except httpx.HTTPError as e:
        raise ProviderUnreachable(
            f"could not reach {c.provider.base_url}: {e}. Leave "
            f"provider.base_url empty to run offline against fixtures/."
        ) from e
    except ValueError as e:
        raise ProviderUnreachable(
            f"{c.provider.base_url} answered with something that is not JSON. "
            f"Is that an OpenAI-compatible endpoint, including /v1?"
        ) from e
    usage = d.get("usage") or {}
    msg = (d.get("choices") or [{}])[0].get("message") or {}

    return Completion(
        text=(msg.get("content") or "").strip(),
        tool_call=_SCENARIO_TOOL.get(scenario),
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        # Most OpenAI-compatible backends do not return a cost, so this is
        # 0.0 against a live endpoint unless a gateway adds it. The budget
        # component therefore reads a real ledger that a real run barely moves:
        # see `capability run --scenario over-budget` for the decision itself.
        cost_usd=float(d.get("cost") or 0.0),
        backend=c.provider.base_url,
        model_used=d.get("model") or resolved,
        reasoning=(msg.get("reasoning_content") or "").strip(),
    )
