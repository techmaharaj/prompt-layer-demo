"""Component 3: token governors.

Budgets are PER CAPABILITY, not per key. "$400 a month on code review" is a
conversation a finance team can have. "$400 a month on Priya" is not.

And the decision that matters is not the limit, it is `on_limit`: what happens
when it runs out. Queue, or deny. Somebody has to decide. In most places
nobody has, which is why the first anyone hears of it is a failed build.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .resolve import Capability

LEDGER = Path(__file__).resolve().parent.parent / "fixtures" / "ledger.json"


@dataclass
class Verdict:
    allowed: bool
    action: str            # "proceed" | "queue" | "deny"
    spent_usd: float
    limit_usd: float
    message: str = ""


def _window_key(window: str) -> str:
    today = date.today()
    return today.strftime("%Y-%m") if window == "monthly" else today.isoformat()


def _read() -> dict:
    if not LEDGER.is_file():
        return {}
    try:
        return json.loads(LEDGER.read_text())
    except json.JSONDecodeError:
        return {}


def spent(cap: Capability) -> float:
    key = f"{cap.name}:{_window_key(cap.budget.window)}"
    return float(_read().get(key, 0.0))


def record_spend(cap: Capability, usd: float) -> None:
    data = _read()
    key = f"{cap.name}:{_window_key(cap.budget.window)}"
    data[key] = round(float(data.get(key, 0.0)) + usd, 6)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def check(cap: Capability, force_over: bool = False) -> Verdict:
    used = cap.budget.limit_usd + 1 if force_over else spent(cap)
    limit = cap.budget.limit_usd
    if used < limit:
        return Verdict(True, "proceed", used, limit)
    if cap.budget.on_limit == "queue":
        return Verdict(
            False,
            "queue",
            used,
            limit,
            f"queued: {cap.name} is at its ${limit:.0f} {cap.budget.window} budget. "
            f"{cap.owner} chose queue, not deny; this runs when the window rolls over.",
        )
    return Verdict(
        False,
        "deny",
        used,
        limit,
        f"denied: {cap.name} is at its ${limit:.0f} {cap.budget.window} budget, "
        f"and {cap.owner} chose deny.",
    )
