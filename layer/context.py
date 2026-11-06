"""Component 2: context templates.

`attach` in the template frontmatter is an ALLOWLIST. Anything not matched is
dropped before the request is assembled, and every drop is RECORDED. A silent
filter is not a control: if you cannot see what was withheld, you cannot
review the decision that withheld it.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


@dataclass
class Assembled:
    prompt: str
    attached: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    redactions: int = 0


def _parse(path: Path) -> tuple[dict, str]:
    m = FRONTMATTER.match(path.read_text())
    if not m:
        raise ValueError(f"{path} has no frontmatter block")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def assemble(template: Path, candidates: list[Path], variables: dict[str, str]) -> Assembled:
    front, body = _parse(template)
    allow: list[str] = list(front.get("attach") or [])
    rules = front.get("redact") or []

    attached: list[Path] = []
    dropped: list[str] = []
    for c in candidates:
        rel = c.name
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(str(c), pat) for pat in allow):
            attached.append(c)
        else:
            # Recorded, not swallowed. This shows up on the span.
            dropped.append(rel)

    blob = "\n\n".join(p.read_text(errors="replace") for p in attached)
    redactions = 0
    for rule in rules:
        pattern, replacement = rule["pattern"], rule.get("replacement", "[redacted]")
        blob, n = re.subn(pattern, replacement, blob)
        redactions += n

    prompt = body
    for key, value in {**variables, "diff": blob, "input": blob}.items():
        prompt = prompt.replace("{{ %s }}" % key, value)

    return Assembled(
        prompt=prompt,
        attached=[p.name for p in attached],
        dropped=dropped,
        redactions=redactions,
    )
