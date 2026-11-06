"""One pass through the layer, as data.

Extracted from cli.py so the CLI and the web page share one implementation.
Returns a Result describing what the five components decided; it is the caller's
job to decide how much of that to show. The developer-facing web page shows
almost none of it, which is the point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import budget, context, permissions, providers, record
from .resolve import ROOT, Capability, UnknownCapability, load

RAW = "__raw__"          # "talk to the model directly", the way it is done today


@dataclass
class Result:
    outcome: str                      # ok | refused | queued | error
    answer: str = ""
    # What the platform returns to a developer when it refuses. Deliberately
    # short: a reason and a reference, like any platform API error.
    message: str = ""
    reference: str = ""               # trace id, so the record can be found
    trace_id: str = ""
    # Everything below is the PLATFORM's view. The web page does not render it.
    capability: str = ""
    owner: str = ""
    model: str = ""
    attached: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    tool: str = ""
    policy_decision: str = ""
    spent_usd: float = 0.0
    limit_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    backend: str = ""


def _files(names: list[str]) -> list[Path]:
    return [ROOT / "context" / n for n in names if (ROOT / "context" / n).is_file()]


def run_raw(prompt: str, files: list[str], model: str,
            backend: str | None = None,
            history: list[tuple[str, str]] | None = None) -> Result:
    """No capability in front of the model. Works fine. Nothing is recorded
    beyond one anonymous inference span."""
    # Everything attached, unfiltered: nothing here decides what may be sent.
    blob = "\n\n".join(p.read_text(errors="replace") for p in _files(files))
    body = _with_history(blob, history, prompt)
    with record.tracer().start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        out = providers.complete(model, body, scenario="happy", backend=backend)
        span.set_attribute("gen_ai.usage.input_tokens", out.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", out.output_tokens)
        trace_id = format(span.get_span_context().trace_id, "032x")

    record.append_boundary_log({
        "path": "raw", "capability": None, "owner": None, "model": model,
        "context_dropped": None, "budget": None, "policy_decision": None,
        "outcome": "ok"})
    return Result(outcome="ok", answer=out.text, model=model, attached=files,
                  trace_id=trace_id, reference=trace_id[:12],
                  input_tokens=out.input_tokens, output_tokens=out.output_tokens,
                  backend=out.backend)


def run_capability(name: str, prompt: str, files: list[str], *,
                   history: list[tuple[str, str]] | None = None,
                   force_over_budget: bool = False,
                   backend: str | None = None) -> Result:
    try:
        cap: Capability = load(name)
    except UnknownCapability as e:
        return Result(outcome="error",
                      message=f"no such capability: {e.name}",
                      capability=e.name)

    with record.capability_span(f"invoke_agent {cap.name}",
                               capability=cap.name, owner=cap.owner) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        ref = trace_id[:12]

        # 2. context: the allowlist decides, and the drop is recorded. The
        # developer is never told; that is the component working, not failing.
        asm = context.assemble(cap.context_path, _files(files), {"prompt": prompt})
        # The conversation so far, so a follow-up means something. Note what is
        # NOT re-decided here: the allowlist above already chose which files may
        # be sent, and history cannot smuggle one back in; it is prior turns
        # of text, not a new attachment.
        asm.prompt = _with_history(asm.prompt, history, prompt)
        span.set_attribute("context.attached", ",".join(asm.attached) or "none")
        span.set_attribute("context.dropped", ",".join(asm.dropped) or "none")
        span.set_attribute("context.redactions", asm.redactions)

        base = Result(outcome="ok", capability=cap.name, owner=cap.owner,
                      model=cap.model, attached=asm.attached, dropped=asm.dropped,
                      trace_id=trace_id, reference=ref)

        # 3. budget
        v = budget.check(cap, force_over=force_over_budget)
        span.set_attribute("budget.limit_usd", v.limit_usd)
        span.set_attribute("budget.spent_usd", v.spent_usd)
        span.set_attribute("budget.action", v.action)
        base.spent_usd, base.limit_usd = v.spent_usd, v.limit_usd
        if not v.allowed:
            span.set_attribute("policy.decision", v.action)
            record.append_boundary_log({
                "path": "capability", "capability": cap.name, "owner": cap.owner,
                "model": cap.model, "context_dropped": asm.dropped,
                "budget": v.action, "policy_decision": None, "outcome": v.action})
            base.outcome = "queued" if v.action == "queue" else "refused"
            base.message = ("This capability has reached its monthly budget. "
                            "Your request is queued and will run when the window "
                            "rolls over."
                            if v.action == "queue"
                            else "This capability has reached its monthly budget.")
            base.policy_decision = v.action
            return base

        # the model
        with record.tracer().start_as_current_span(f"chat {cap.model}") as inf:
            inf.set_attribute("gen_ai.operation.name", "chat")
            inf.set_attribute("gen_ai.request.model", cap.model)
            scenario = "out-of-scope-write" if prompt_wants_action(prompt) else "happy"
            out = providers.complete(cap.model, asm.prompt, scenario=scenario,
                                     backend=backend)
            inf.set_attribute("gen_ai.usage.input_tokens", out.input_tokens)
            inf.set_attribute("gen_ai.usage.output_tokens", out.output_tokens)
        budget.record_spend(cap, out.cost_usd)
        base.answer = out.text
        base.input_tokens, base.output_tokens = out.input_tokens, out.output_tokens
        base.backend = out.backend

        # 4. permissions
        if out.tool_call:
            tool = out.tool_call["name"]
            base.tool = tool
            try:
                d = permissions.evaluate(cap, tool, out.tool_call.get("arguments", {}))
            except permissions.KyvernoMissing as e:
                base.outcome = "error"
                base.message = str(e)
                return base
            with record.tracer().start_as_current_span(f"execute_tool {tool}") as ts:
                ts.set_attribute("gen_ai.tool.name", tool)
                ts.set_attribute("gen_ai.tool.call.id", out.tool_call.get("id", "call_1"))
                ts.set_attribute("gen_ai.tool.type", "function")
                ts.set_attribute("policy.decision", "allowed" if d.allowed else "denied")
                if not d.allowed:
                    record.mark_denied(ts, d.reason)
            span.set_attribute("policy.decision", "allowed" if d.allowed else "denied")
            base.policy_decision = "allowed" if d.allowed else "denied"
            if not d.allowed:
                base.outcome = "refused"
                base.message = d.reason
        else:
            span.set_attribute("policy.decision", "not-applicable")

        record.append_boundary_log({
            "path": "capability", "capability": cap.name, "owner": cap.owner,
            "model": cap.model, "context_dropped": asm.dropped,
            "budget": v.action, "policy_decision": base.policy_decision or None,
            "outcome": base.outcome})
        return base


# Whether the developer is asking the capability to ACT on something, rather
# than to answer a question. Only unambiguous multi-word phrases: single verbs
# are too loose: "deploy" matched "our deploy vars" and turned a question
# about a file into a request to change production.
#
# A real layer would read the model's tool calls. Local models emit those
# inconsistently, and this component is about the DECISION, not the parsing.
_ACTION_PHRASES = (
    "stop traffic", "take it down", "take down", "shut it down", "shut down",
    "scale it", "scale the", "scale down", "to zero", "roll back", "rollback",
    "restart the", "restart it", "disable the", "drain the", "kill the",
)


def prompt_wants_action(prompt: str) -> bool:
    p = prompt.lower()
    return any(w in p for w in _ACTION_PHRASES)


def _with_history(body: str, history: list[tuple[str, str]] | None,
                  current: str) -> str:
    """Prepend the conversation so far, newest last.

    Kept short and trimmed hard. A long thread would quietly inflate every
    request, and token budgets are a component of this repo rather than an
    afterthought in it. A backend serving one request at a time pays for every
    extra token of prompt twice.
    """
    if not history:
        return f"{body}\n\nThe developer asks: {current}"
    lines = []
    for role, text in history[-4:]:
        who = "Developer" if role == "you" else "You previously answered"
        lines.append(f"{who}: {text.strip()[:350]}")
    return (f"{body}\n\nEarlier in this conversation:\n"
            + "\n\n".join(lines)
            + f"\n\nThe developer now asks: {current}")
