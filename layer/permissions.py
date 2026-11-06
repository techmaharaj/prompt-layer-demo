"""Component 4: agent permissions, boundary-side.

The model has come back asking to call a tool. Before that call leaves the
layer, the request is handed to the same policy engine the platform already
uses for admission control (Kyverno) in JSON mode, against the tool-call
payload.

Two refusal points exist in this repo and the pairing is deliberate:

  * here, boundary-side: the layer refuses what the capability was never
    allowed to ask for;
  * policy/admission.yaml, cluster-side: the cluster refuses it anyway, as
    real admission control, if the layer is wrong or was bypassed.

JSON mode is evaluated by the Kyverno CLI today; the engine SDK is planned,
not shipped. That is why this is a subprocess and not a library call.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .resolve import Capability


class KyvernoMissing(RuntimeError):
    pass


@dataclass
class Decision:
    allowed: bool
    reason: str
    policy: str
    raw: str = ""


def payload(cap: Capability, tool_name: str, arguments: dict) -> dict:
    """What gets handed to the policy engine. Deliberately flat and readable:
    a payload you cannot read is a policy nobody will review."""
    return {
        "capability": {"name": cap.name, "owner": cap.owner, "tools": cap.tools},
        "tool_call": {"name": tool_name, "arguments": arguments},
    }


def evaluate(cap: Capability, tool_name: str, arguments: dict) -> Decision:
    kyverno = shutil.which("kyverno")
    if not kyverno:
        raise KyvernoMissing(
            "the kyverno CLI is not on PATH. JSON-mode ValidatingPolicy is "
            "CLI-evaluated today (no engine SDK yet), so the boundary check "
            "needs it. See README prerequisites."
        )

    body = payload(cap, tool_name, arguments)
    with tempfile.TemporaryDirectory() as tmp:
        pf = Path(tmp) / "payload.json"
        pf.write_text(json.dumps(body, indent=2))
        proc = subprocess.run(
            [kyverno, "apply", str(cap.policy_path), "--json", str(pf)],
            capture_output=True,
            text=True,
        )
    out = (proc.stdout or "") + (proc.stderr or "")

    # Verified against Kyverno CLI v1.19.2 on 2026-11-04:
    #   `kyverno apply <policy> --json <payload>` exits 1 when a validation
    #   fails and 0 when all pass, and prints a "pass: N, fail: N, ..." summary.
    # The exit code is the signal; the summary line is the cross-check, so a
    # future CLI change that breaks one does not silently pass the other.
    fail_count = 0
    if m := re.search(r"fail:\s*(\d+)", out):
        fail_count = int(m.group(1))
    denied = proc.returncode != 0 or fail_count > 0

    if denied and proc.returncode == 0 and fail_count == 0:
        raise RuntimeError(f"could not read a verdict out of the kyverno CLI:\n{out}")

    reason = ""
    for line in out.splitlines():
        if "denied:" in line:
            reason = line.split("denied:", 1)[1].strip()
            break
    return Decision(
        allowed=not denied,
        reason=reason or ("allowed by policy" if not denied else "denied by policy"),
        policy=cap.policy_path.name,
        raw=out.strip(),
    )
