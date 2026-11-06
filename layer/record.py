"""Component 5: AI observability.

One trace, not another log. The whole point of this file is that the four
decisions taken by the other four components end up as attributes on ONE span
tree, so the record of a crossing is a single thing you can look at and a
single thing you can audit.

Attribute naming, checked 2026-11-04:
  * inference span is named `{gen_ai.operation.name} {gen_ai.request.model}`
  * tool span is named `execute_tool {gen_ai.tool.name}` and carries
    gen_ai.tool.name / .call.id / .type
  * model and token usage belong on the INFERENCE span, never on the tool span
  * everything under gen_ai.* is still `Development` stability. In semconv
    v1.42.0 (12 June 2026) the GenAI conventions were deprecated in the main
    repo and moved to open-telemetry/semantic-conventions-genai, which has
    published no tagged release. Expect these names to move.

The three custom attributes are the ones a gateway's own logs do not have,
and they are the reason this record is a platform artefact rather than a bill:
  capability.name, capability.owner, policy.decision
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode

BOUNDARY_LOG = Path(__file__).resolve().parent.parent / "fixtures" / "boundary-log.jsonl"
_tracer: trace.Tracer | None = None


def tracer() -> trace.Tracer:
    global _tracer
    if _tracer is not None:
        return _tracer

    provider = TracerProvider(
        resource=Resource.create({"service.name": "prompt-layer", "service.version": "0.1.0"})
    )
    from . import config as _cfg

    endpoint = (_cfg.load().otlp_endpoint
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
    elif os.environ.get("PROMPT_LAYER_CONSOLE_SPANS"):
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("prompt-layer")
    return _tracer


@contextmanager
def capability_span(name: str, *, capability: str, owner: str):
    with tracer().start_as_current_span(name) as span:
        span.set_attribute("capability.name", capability)
        span.set_attribute("capability.owner", owner)
        yield span


def mark_denied(span, reason: str) -> None:
    span.set_attribute("policy.decision", "denied")
    span.set_status(Status(StatusCode.ERROR, reason))


def append_boundary_log(entry: dict) -> None:
    """Flat, append-only record the web page reads. Not a replacement for the
    trace, a convenience so the five decisions are legible without driving
    Jaeger."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    BOUNDARY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BOUNDARY_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
