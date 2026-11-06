"""Deployment config: where the model comes from, and what it's called here.

Separate from capabilities/*.yaml on purpose. A capability is a decision your
platform owns and publishes. This file is just plumbing for one install, which
is why it's gitignored and why config.example.yaml is the committed thing.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"


@dataclass
class Provider:
    base_url: str = ""
    api_key: str = ""
    timeout: float = 180.0
    thinking: bool = False
    # The model the playground offers. Only used by the ungoverned side; the
    # capabilities declare their own.
    playground_model: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


@dataclass
class Config:
    provider: Provider = field(default_factory=Provider)
    models: dict[str, str] = field(default_factory=dict)
    namespace: str = "demo-prod"
    # Both optional, and both passed through to every kubectl call this repo
    # makes. Empty means "whatever the current context is", which is the normal
    # case on a laptop with one cluster.
    kubeconfig: str = ""
    context: str = ""
    otlp_endpoint: str = ""
    trace_ui: str = ""

    def resolve_model(self, logical: str) -> str:
        """A capability names a model; this is where one install maps it to
        whatever its backend actually serves. Empty map = pass through, which
        is the normal case: the capability already names the real thing."""
        return self.models.get(logical) or logical


def load() -> Config:
    if not CONFIG.is_file():
        return Config()
    raw = yaml.safe_load(CONFIG.read_text()) or {}
    p = raw.get("provider") or {}

    key = p.get("api_key") or ""
    if not key and (env := p.get("api_key_env")):
        key = os.environ.get(env, "")

    cl = raw.get("cluster") or {}
    ob = raw.get("observability") or {}
    return Config(
        provider=Provider(
            base_url=(p.get("base_url") or "").strip(),
            api_key=key,
            timeout=float(p.get("timeout") or 180),
            thinking=bool(p.get("thinking")),
            playground_model=(p.get("playground_model") or "").strip(),
        ),
        models={k: v for k, v in (raw.get("models") or {}).items() if v},
        namespace=cl.get("namespace") or "demo-prod",
        kubeconfig=cl.get("kubeconfig") or "",
        context=cl.get("context") or "",
        otlp_endpoint=(ob.get("otlp_endpoint") or "").strip(),
        trace_ui=(ob.get("trace_ui") or "").strip(),
    )


@functools.lru_cache(maxsize=1)
def kubectl_cmd() -> list[str] | None:
    """How to reach the cluster from here, or None if nothing answers.

    Resolution order, and all three failure modes are deliberate:

      1. $KUBECTL, if set. The escape hatch, honoured verbatim.
      2. plain `kubectl`, if it actually reaches a cluster.
      3. `sudo -n k3s kubectl`, for a single-node k3s whose kubeconfig is
         root-only. `-n` is the important part: never prompt. A hung password
         prompt in the middle of a self-test looks like a broken repo.

    None means there is no cluster, which is a supported state: the admission
    floor and the trace view are the only parts that need one.
    """
    c = load()
    flags: list[str] = []
    if c.kubeconfig:
        flags += ["--kubeconfig", c.kubeconfig]
    if c.context:
        flags += ["--context", c.context]

    if env := os.environ.get("KUBECTL"):
        return env.split() + flags

    def reaches(cmd: list[str]) -> bool:
        try:
            return subprocess.run(cmd + ["version", "--request-timeout=3s"],
                                  capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if shutil.which("kubectl") and reaches(["kubectl", *flags]):
        return ["kubectl", *flags]
    if shutil.which("k3s") and reaches(["sudo", "-n", "k3s", "kubectl", *flags]):
        return ["sudo", "-n", "k3s", "kubectl", *flags]
    return None


def playground_model() -> str:
    """What the ungoverned side sends.

    From config if set, otherwise whatever the first capability declares, so a
    fresh clone works without naming a model twice.
    """
    from .resolve import catalogue

    c = load()
    if c.provider.playground_model:
        return c.provider.playground_model
    caps = catalogue()
    # Last resort only, for a clone whose capabilities/ is empty. Any real
    # install answers from config or from the first capability above.
    return caps[0].model if caps else "gpt-4o-mini"
