"""`capability`: what a developer touches.

Each command prints what the layer decided and nothing more: the output is a
record, not a tour.

    capability list                    the catalogue
    capability show code-review        the artefact
    capability run code-review         ask for it; no model named
    capability run deploy-to-prod      not in the catalogue
    capability run code-review --scenario <s>
    capability bypass code-review      no capability in front of the model
    capability reset                   back to the starting point
    capability verify                  the self-test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import budget, context, permissions, providers, record
from .resolve import ROOT, Capability, UnknownCapability, catalogue, load

SCENARIOS = {
    "happy": "everything passes; five decisions, one trace",
    "secret-in-context": "a .env is offered as context and the allowlist drops it",
    "over-budget": "the capability is at its budget; on_limit decides what happens",
    "out-of-scope-write": "the model asks for a tool the capability may not call",
}

DEFAULT_DIFF = ROOT / "context" / "fix.patch"

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def _p(s: str = "") -> None:
    print(s, flush=True)


def cmd_list(_: argparse.Namespace) -> int:
    caps = catalogue()
    _p()
    _p(f"{BOLD}{'CAPABILITY':<22}{'OWNER':<34}{'MODEL':<16}{'BUDGET':<10}ON LIMIT{OFF}")
    for c in caps:
        _p(f"{c.name:<22}{c.owner:<34}{c.model:<16}"
           f"${c.budget.limit_usd:<9.0f}{c.budget.on_limit}")
    _p()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        cap = load(args.name)
    except UnknownCapability as e:
        return _unowned(e)
    _p((ROOT / "capabilities" / f"{cap.name}.yaml").read_text())
    return 0


def _unowned(e: UnknownCapability) -> int:
    _p()
    _p(f"{RED}not in the catalogue: {e.name}{OFF}")
    _p("  no owner, no declared model, no context allowlist, no budget, no policy")
    _p(f"  declared: {', '.join(e.known)}")
    _p(f"  to add one: capabilities/{e.name}.yaml  (needs an owner)")
    _p()
    return 4


def _run_bypass(args: argparse.Namespace) -> int:
    """Same task, no capability in front of the model. It works, and that is the
    point. What is missing is visible only in the record."""
    from . import config as _c

    model = args.model or _c.playground_model()
    diff = Path(args.diff).read_text() if args.diff else ""
    _p()
    _p(f"bypass      {model:<22}named by the developer")

    with record.tracer().start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        out = providers.complete(model, diff, scenario="happy", backend=args.backend)
        span.set_attribute("gen_ai.usage.input_tokens", out.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", out.output_tokens)

    record.append_boundary_log({
        "path": "bypass", "capability": None, "owner": None, "model": model,
        "context_dropped": None, "budget": None, "policy_decision": None,
        "outcome": "ok"})
    _p(f"result      {'ok':<22}{out.input_tokens}+{out.output_tokens} tok")
    _p("owner       -")
    _p("budget      -")
    _p("allowlist   -")
    _p("policy      -")
    _p()
    if out.text.strip():
        _p(out.text.strip()[:600])
        _p()
    return 0


def _run_capability(args: argparse.Namespace) -> int:
    try:
        cap: Capability = load(args.name)
    except UnknownCapability as e:
        return _unowned(e)

    scenario = args.scenario
    exit_code = 0
    _p()
    _p(f"capability  {cap.name:<22}owner  {cap.owner}")

    with record.capability_span(
        f"invoke_agent {cap.name}", capability=cap.name, owner=cap.owner
    ) as span:
        # 2. context
        candidates = [Path(args.diff)] if args.diff else []
        if scenario == "secret-in-context":
            candidates.append(ROOT / "context" / "deploy-vars.env")
        asm = context.assemble(cap.context_path, candidates, {})
        span.set_attribute("context.attached", ",".join(asm.attached) or "none")
        span.set_attribute("context.dropped", ",".join(asm.dropped) or "none")
        span.set_attribute("context.redactions", asm.redactions)
        drop = f"{RED}{','.join(asm.dropped)}{OFF}" if asm.dropped else "-"
        _p(f"context     {','.join(asm.attached) or '-':<22}dropped  {drop}")

        # 3. budget
        verdict = budget.check(cap, force_over=(scenario == "over-budget"))
        span.set_attribute("budget.limit_usd", verdict.limit_usd)
        span.set_attribute("budget.spent_usd", verdict.spent_usd)
        span.set_attribute("budget.action", verdict.action)
        money = f"${verdict.spent_usd:.2f} / ${verdict.limit_usd:.0f} {cap.budget.window}"
        money = f"{money:<22}" if len(money) < 22 else money + "  "
        if not verdict.allowed:
            span.set_attribute("policy.decision", verdict.action)
            _p(f"budget      {money}{YELLOW}{verdict.action.upper()}{OFF}  "
               f"(on_limit: {cap.budget.on_limit})")
            record.append_boundary_log({
                "path": "capability", "capability": cap.name, "owner": cap.owner,
                "model": cap.model, "context_dropped": asm.dropped,
                "budget": verdict.action, "policy_decision": None,
                "outcome": verdict.action})
            _p()
            return 3
        _p(f"budget      {money}(on_limit: {cap.budget.on_limit})")

        # 1+. the model
        with record.tracer().start_as_current_span(f"chat {cap.model}") as inf:
            inf.set_attribute("gen_ai.operation.name", "chat")
            inf.set_attribute("gen_ai.request.model", cap.model)
            out = providers.complete(cap.model, asm.prompt, scenario=scenario,
                                     backend=args.backend)
            inf.set_attribute("gen_ai.usage.input_tokens", out.input_tokens)
            inf.set_attribute("gen_ai.usage.output_tokens", out.output_tokens)
        budget.record_spend(cap, out.cost_usd)
        label = out.backend
        if "://" in label:
            label = label.split("://", 1)[1].split("/", 1)[0]
        _p(f"model       {cap.model:<22}via  {label}  "
           f"{out.input_tokens}+{out.output_tokens} tok")

        # 4. permissions
        decision = None
        if out.tool_call:
            tool = out.tool_call["name"]
            try:
                decision = permissions.evaluate(cap, tool, out.tool_call.get("arguments", {}))
            except permissions.KyvernoMissing as e:
                _p(f"policy      {RED}cannot evaluate{OFF}  {e}")
                return 5
            with record.tracer().start_as_current_span(f"execute_tool {tool}") as ts:
                ts.set_attribute("gen_ai.tool.name", tool)
                ts.set_attribute("gen_ai.tool.call.id", out.tool_call.get("id", "call_1"))
                ts.set_attribute("gen_ai.tool.type", "function")
                ts.set_attribute("policy.decision",
                                 "allowed" if decision.allowed else "denied")
                if not decision.allowed:
                    record.mark_denied(ts, decision.reason)
            span.set_attribute("policy.decision",
                               "allowed" if decision.allowed else "denied")
            if decision.allowed:
                _p(f"policy      {tool:<22}{GREEN}ALLOWED{OFF}  ({decision.policy})")
            else:
                _p(f"policy      {tool:<22}{RED}DENIED{OFF}   ({decision.policy})")
                _p(f"            {RED}{decision.reason}{OFF}")
                exit_code = 2
        else:
            span.set_attribute("policy.decision", "not-applicable")
            _p(f"policy      {'-':<22}no tool call this turn")

        record.append_boundary_log({
            "path": "capability", "capability": cap.name, "owner": cap.owner,
            "model": cap.model, "context_dropped": asm.dropped,
            "budget": verdict.action,
            "policy_decision": (None if decision is None
                                else ("allowed" if decision.allowed else "denied")),
            "outcome": "denied" if exit_code == 2 else "ok"})

    if exit_code == 0 and out.text.strip():
        _p()
        _p(out.text.strip()[:900])
    _p()
    return exit_code


def cmd_reset(_: argparse.Namespace) -> int:
    """Back to the starting point. Run it between demos."""
    import subprocess

    from . import config as _c

    c = _c.load()
    kubectl = _c.kubectl_cmd()

    def kc(*args, timeout=180):
        """No cluster is a supported state, so this returns a failure rather
        than raising when there is nothing to talk to."""
        if kubectl is None:
            return subprocess.CompletedProcess(args, 1, "", "no cluster")
        try:
            return subprocess.run(list(kubectl) + list(args),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 1, "", "timed out")

    for f in (ROOT / "fixtures" / "ledger.json", ROOT / "fixtures" / "boundary-log.jsonl"):
        f.unlink(missing_ok=True)
    _p()
    _p("boundary    cleared")

    # The starting point includes one capability already at its budget, so the
    # "what happens when it runs out" outcome is reachable from the developer's
    # page without passing a flag. Same idea as the fixture workload in the
    # cluster: reset establishes the state, it does not fake the check; the
    # budget component really reads this ledger and really refuses.
    spent = load("summarise-incident")
    budget.record_spend(spent, spent.budget.limit_usd)
    _p(f"ledger      cleared, {spent.name} seeded at its ${spent.budget.limit_usd:.0f} limit")

    # Jaeger all-in-one keeps traces in memory and has NO purge API, so the only
    # way to clear old traces, and stale service names, out of the trace view
    # is to restart the pod. Worth waiting for ready: spans sent while it is down
    # are simply lost, and the first thing after a reset is usually a run whose
    # trace you want.
    if c.otlp_endpoint:
        r = kc("-n", "observability", "rollout", "restart", "deploy/jaeger")
        if r.returncode == 0:
            kc("-n", "observability", "rollout", "status", "deploy/jaeger",
               "--timeout=120s")
            _p("traces      cleared (jaeger restarted)")
        else:
            _p(f"traces      {DIM}jaeger not found; skipped{OFF}")

    r = kc("-n", c.namespace, "scale", "deploy/checkout", "--replicas=1")
    if r.returncode == 0:
        kc("-n", c.namespace, "rollout", "status", "deploy/checkout", "--timeout=120s")
        out = kc("-n", c.namespace, "get", "deploy", "checkout", "--no-headers")
        _p(f"checkout    {out.stdout.strip() or 'restored'}")
    else:
        _p(f"checkout    {DIM}no cluster; skipped{OFF}")
    _p()
    return 0


def cmd_scenarios(_: argparse.Namespace) -> int:
    _p()
    _p(f"{BOLD}SCENARIO{OFF}              {BOLD}WHAT IT SHOWS{OFF}")
    for k, v in sorted(SCENARIOS.items()):
        _p(f"{k:<22}{v}")
    _p()
    _p(f"{DIM}  capability run code-review --scenario <name>{OFF}")
    _p()
    return 0


def cmd_verify(_: argparse.Namespace) -> int:
    from scripts.selftest import main as selftest

    return selftest()


def cmd_run(args: argparse.Namespace) -> int:
    if args.bypass:
        return _run_bypass(args)
    return _run_capability(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="capability", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="the catalogue: every capability and its owner").set_defaults(
        func=cmd_list
    )

    sh = sub.add_parser("show", help="print a capability definition")
    sh.add_argument("name")
    sh.set_defaults(func=cmd_show)

    rn = sub.add_parser("run", help="ask for a capability")
    rn.add_argument("name")
    rn.add_argument("--diff", default=str(DEFAULT_DIFF),
                    help=f"diff to review (default: {DEFAULT_DIFF.name})")
    rn.add_argument("--scenario", choices=sorted(SCENARIOS), default="happy",
                    help="; ".join(f"{k}: {v}" for k, v in SCENARIOS.items()))
    rn.add_argument("--bypass", action="store_true",
                    help="skip the layer: straight at the model, developer names it")
    rn.add_argument("--model", help="only with --bypass; default: the playground's")
    rn.add_argument("--backend", help="fixture (default) or live")
    rn.set_defaults(func=cmd_run)

    sub.add_parser("scenarios", help="list the scenarios and what each one shows").set_defaults(
        func=cmd_scenarios
    )

    bp = sub.add_parser(
        "bypass", help="same task, straight at the model, no capability in front of it"
    )
    bp.add_argument("name", nargs="?", default="code-review")
    bp.add_argument("--diff", default=str(DEFAULT_DIFF))
    bp.add_argument("--model", help="default: whatever the playground offers")
    bp.add_argument("--backend")
    bp.set_defaults(func=_run_bypass, bypass=True, scenario="happy")

    sub.add_parser("reset", help="back to the starting point").set_defaults(func=cmd_reset)
    sub.add_parser("verify", help="the self-test; non-zero exit if anything half-worked").set_defaults(
        func=cmd_verify
    )

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except providers.ProviderUnreachable as e:
        # The backend, not the layer. Say which, and do not print a traceback at
        # somebody who has just mistyped a base_url.
        print(f"\nbackend    {RED}unreachable{OFF}  {e}\n", file=sys.stderr)
        return 6
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
