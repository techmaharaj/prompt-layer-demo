"""Component 1: the capability interface.

A developer asks for a capability by name. They do not name a model. The
decision about which model, which context template and which evals was taken
once, by somebody, and it is written down in capabilities/<name>.yaml.

The interesting path in this file is the one where the capability does NOT
exist. That is not an error case to tidy away. It is the whole point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CAPABILITY_DIR = ROOT / "capabilities"


class UnknownCapability(Exception):
    """Asked for something nobody has decided."""

    def __init__(self, name: str, known: list[str]):
        self.name = name
        self.known = known
        super().__init__(name)


@dataclass
class Budget:
    limit_usd: float
    window: str = "monthly"
    on_limit: str = "queue"


@dataclass
class Capability:
    name: str
    owner: str
    model: str
    context: str
    evals: str
    budget: Budget
    policy: str
    tools: list[str] = field(default_factory=list)
    match: list[str] = field(default_factory=list)

    @property
    def context_path(self) -> Path:
        return ROOT / self.context

    @property
    def policy_path(self) -> Path:
        return ROOT / self.policy


def catalogue() -> list[Capability]:
    return sorted(
        (load(p.stem) for p in CAPABILITY_DIR.glob("*.yaml")),
        key=lambda c: c.name,
    )


def known_names() -> list[str]:
    return sorted(p.stem for p in CAPABILITY_DIR.glob("*.yaml"))


def load(name: str) -> Capability:
    path = CAPABILITY_DIR / f"{name}.yaml"
    if not path.is_file():
        raise UnknownCapability(name, known_names())
    raw = yaml.safe_load(path.read_text())
    budget = Budget(**(raw.get("budget") or {"limit_usd": 0.0}))
    return Capability(
        name=raw["name"],
        owner=raw.get("owner", ""),
        model=raw["model"],
        context=raw["context"],
        evals=raw.get("evals", ""),
        budget=budget,
        policy=raw.get("policy", "policy/tool-call.yaml"),
        tools=list(raw.get("tools") or []),
        match=[str(m).lower() for m in (raw.get("match") or [])],
    )


def route(prompt: str) -> Capability | None:
    """Which capability covers what this developer just asked for.

    The developer never picks one. They say what they want; the platform decides
    what that maps to, and when nothing matches, that is not a failure to
    handle, it is the catalogue telling you nobody has decided it yet.
    """
    p = f" {prompt.lower()} "
    best, score = None, 0
    for cap in catalogue():
        hits = sum(len(w) for w in cap.match if w in p)
        if hits > score:
            best, score = cap, hits
    return best
