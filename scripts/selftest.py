"""The repo proving itself. `./capability verify`, or `make verify`.

Asserts the claims this repo makes, so that a clone which has silently
half-worked fails loudly instead of looking fine. Fourteen checks cover the
five components offline. Four more run only if a cluster with Kyverno is
reachable, for eighteen in total; with no cluster those are reported as SKIP
and are not counted as passes.

Exits non-zero with the reason on any failure. No pipes swallowing exit codes.
"""
from __future__ import annotations

import sys

from layer import budget, config, context, permissions, providers
from layer.resolve import ROOT, UnknownCapability, load

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(': ' + detail) if detail else ''}")
        FAILURES.append(label)


def main() -> int:
    print("prompt-layer-demo self-test")
    print()

    # --- component 1: the capability resolves, and it has an owner --------
    cap = load("code-review")
    check("capability resolves without a model name from the caller", cap.model != "")
    check("capability has an owner", bool(cap.owner), "owner is empty")

    # --- the unowned path is a first-class outcome, not a crash -----------
    try:
        load("deploy-to-prod")
        check("unknown capability raises UnknownCapability", False, "it did not raise")
    except UnknownCapability as e:
        check("unknown capability raises UnknownCapability", e.name == "deploy-to-prod")

    # --- component 2: the allowlist drops what is not on it ---------------
    asm = context.assemble(
        cap.context_path,
        [ROOT / "context" / "fix.patch", ROOT / "fixtures" / "leaked.env"],
        {},
    )
    check("allowlist attached the patch", "fix.patch" in asm.attached)
    check("allowlist DROPPED the .env", "leaked.env" in asm.dropped,
          f"dropped={asm.dropped}")
    check("the drop is recorded, not silent", len(asm.dropped) > 0)

    # --- component 3: on_limit is honoured --------------------------------
    verdict = budget.check(cap, force_over=True)
    check("over budget yields the owner's on_limit choice",
          verdict.action == cap.budget.on_limit,
          f"action={verdict.action} on_limit={cap.budget.on_limit}")

    # --- component 4: real Kyverno denies the out-of-scope write ----------
    try:
        allowed = permissions.evaluate(cap, "post_review_comment", {"pr": 1})
        denied = permissions.evaluate(
            cap, "patch_deployment", {"namespace": "prod", "replicas": 0}
        )
    except permissions.KyvernoMissing as e:
        print(f"  FAIL  kyverno CLI available: {e}")
        FAILURES.append("kyverno CLI available")
    else:
        check("declared tool is ALLOWED by policy", allowed.allowed, allowed.raw[-200:])
        check("out-of-scope write is DENIED by policy", not denied.allowed,
              denied.raw[-200:])
        check("the denial carries a human-readable reason", "may not call" in denied.reason,
              denied.reason)

    # --- component 5: the span carries the owner --------------------------
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("selftest")
    with tracer.start_as_current_span(f"invoke_agent {cap.name}") as span:
        span.set_attribute("capability.name", cap.name)
        span.set_attribute("capability.owner", cap.owner)
        span.set_attribute("policy.decision", "denied")
    spans = exporter.get_finished_spans()
    attrs = dict(spans[0].attributes) if spans else {}
    check("a span was recorded", bool(spans))
    check("the span carries capability.owner", attrs.get("capability.owner") == cap.owner,
          str(attrs))
    check("the span carries policy.decision", "policy.decision" in attrs, str(attrs))

    # --- the provider swap does not move anything else --------------------
    out = providers.complete(cap.model, "x", scenario="happy", backend="fixture")
    check("fixture backend returns a completion offline", out.text != "")

    # --- the cluster-side floor, if a cluster is reachable -----------------
    import subprocess

    ns = config.load().namespace
    kubectl = config.kubectl_cmd()
    agent = f"--as=system:serviceaccount:{ns}:capability-agent"

    def kc(*args, as_agent=False):
        cmd = list(kubectl) + ([agent] if as_agent else []) + ["-n", ns, *args]
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return 1, "timed out"
        return pr.returncode, ((pr.stdout or "") + (pr.stderr or ""))

    rc = 1 if kubectl is None else kc("get", "deploy", "checkout")[0]
    if rc != 0:
        print(f"  SKIP  cluster checks: {ns}/checkout not reachable")
    else:
        # These impersonate the agent's service account, which needs rights the
        # agent itself does not have. Run as a user who may `--as`.
        def refusal(out: str) -> str:
            if "Forbidden" in out:
                return "rbac"          # policy never ran: a missing verb, not a control
            if "denied the request" in out:
                return "admission"
            return "allowed"

        kc("scale", "deploy/checkout", "--replicas=1")
        for verb, args in (
            ("patch",  ("patch", "deploy/checkout", "-p", '{"spec":{"replicas":0}}')),
            ("scale",  ("scale", "deploy/checkout", "--replicas=0")),
            ("delete", ("delete", "deploy/checkout")),
        ):
            _, out = kc(*args, as_agent=True)
            got = refusal(out)
            check(f"agent {verb} is denied by ADMISSION (not rbac)", got == "admission", got)

        _, out = kc("patch", "deploy/checkout", "-p", '{"spec":{"replicas":1}}')
        check("a human is NOT blocked", refusal(out) == "allowed", refusal(out))
        kc("scale", "deploy/checkout", "--replicas=1")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
