# The Prompt Layer

Two tools that do the same job. One is a playground: pick a model, paste in
whatever you have, get an answer. The other is an assistant: ask for an outcome
and a *capability* handles it.

Both talk to the same gateway, the same model, the same key. Run them side by
side and the difference is not in the answers. It is in what each one leaves
behind.

## What it shows

| Step | What happens | Result |
|---|---|---|
| **Playground** | Attach a diff and a file of deploy variables, ask for a review, then ask it to quote a secret from that file | It quotes the key back to you. Nothing decided it should not |
| **Assistant** | Same prompt, same two files, then the same question about the secrets file | A real review, then `I don't have that in what was attached.` The file never reached the model |
| **The gateway** | Compare the two rows in the gateway's log | Same key, same model, similar tokens. Nothing in them says which request was governed |
| **The trace** | Open the assistant's trace | `capability.owner`, `context.dropped`, `budget.action`, `policy.decision`, on one span tree |
| **Asking it to act** | Ask the assistant to fix the bug *and stop traffic to checkout* | Refused at the boundary, with a reference that links to the trace. Refused again at the cluster if the layer is bypassed |
| **The gap** | Ask for something nobody has defined, like `deploy this to prod` | No capability covers it. That is the honest state of most of this work |

## The idea

Most teams already have the tooling. An AI gateway gives you virtual keys,
budgets, model aliases, redaction, tool permissions and request logs. It is good,
and this does not replace it: every call here goes through a gateway.

But a gateway's units are **keys** and **models**. It can tell you a call
happened, what it cost and which key made it. It cannot tell you what the call
was *for*, who decided that this is how your company reviews code, whether a file
should have been sent, or whether an action should have been allowed.

Those are not features it is missing. They are decisions nobody has made, and a
gateway cannot make them for you. It will run perfectly with an empty list of
approved uses, forever, and never mention it.

A **capability** is where one of those decisions lives:

```yaml
name: code-review
owner: platform-team@example.com
model: qwen3.8-27b
context: templates/code-review.md
evals: evals/code-review/
```

Five lines, and the important one is `owner`. Somebody is accountable for which
model reviews your code, what may be sent with it, what it costs and what it is
allowed to do. A developer asks for `code-review`. They never pick a model.

### Why this one is different

Every other capability a platform offers converges on a state you declared. Apply
the same spec twice, get the same database. This one does not: same prompt,
twice, different answer. That is not a bug, it is what the thing is.

So you cannot govern it by declaring desired state. You govern it by constraining
the boundary and recording the crossing.

## How it works

| # | Component | What it constrains | Where |
|---|---|---|---|
| 1 | Capability interface | what can be **asked for** | `capabilities/*.yaml`, `layer/resolve.py` |
| 2 | Context templates | what gets **sent** | `templates/*.md`, `layer/context.py` |
| 3 | Token governors | how **much** | `layer/budget.py` |
| 4 | Agent permissions | what it may **do** | `policy/*.yaml`, `layer/permissions.py` |
| 5 | Observability | what **crossed**, recorded | `layer/record.py` |

One file per component, on purpose.

A request can be refused in two places. At the boundary, `policy/tool-call.yaml`
is a Kyverno `ValidatingPolicy` in JSON mode that checks a tool call before it
leaves. At the cluster, `policy/admission.yaml` is a `ClusterPolicy` that refuses
the same write at the Kubernetes API even if the layer is wrong or bypassed.

> The layer refuses what it was told not to ask for.
> The cluster refuses it anyway if the layer is wrong.

The cluster rule is scoped to the agent's identity, so a human on call can still
scale things during an incident. The point is governing the agent, not freezing the cluster.

## What's in this repo

```
capabilities/
  code-review.yaml         the artefact: a name, an owner, and decisions already taken
  summarise-incident.yaml  three more, so the catalogue reads like a catalogue
templates/
  code-review.md           what may be sent with it. `attach:` is an allowlist
policy/
  tool-call.yaml           boundary side: Kyverno ValidatingPolicy, JSON mode, over a tool call
  admission.yaml           cluster side: real admission control, scoped to the agent
layer/
  resolve.py               1. find a capability, and route a request to one
  context.py               2. assemble context, drop what is not allowed, record the drop
  budget.py                3. per capability spend, and what happens at the limit
  permissions.py           4. ask Kyverno whether a tool call may happen
  record.py                5. one trace carrying all five decisions
  providers.py             one OpenAI compatible adapter for any backend
  engine.py                one pass through the layer, as data
  web.py                   the two web tools: /playground and /assistant
  cli.py                   the same thing from a terminal
  config.py                reads config.yaml
before/
  review.sh                how this gets done today: 40 lines, no layer at all
context/                   a developer's folder, including a file they should not send
fixtures/                  canned responses, so it runs with no backend at all
evals/                     structure only, and evals/README.md says why
k8s/
  demo-prod.yaml           a fixture workload for an agent to try to break
  jaeger.yaml              Jaeger on fixed NodePorts: 30686 UI, 30318 OTLP in
scripts/
  selftest.py              the assertions behind `capability verify`
  review-page.service      systemd unit that keeps the web tools up
setup.sh                   one-time setup, ending in the self-test
capability                 the CLI: list, show, run, scenarios, bypass, reset, verify
config.example.yaml        copy to config.yaml: endpoint, key, model map, trace UI
```

## Run it yourself

### 1. Point it at a model

```bash
cp config.example.yaml config.yaml
```

Two fields matter:

```yaml
provider:
  base_url: "https://your-endpoint/v1"   # any OpenAI compatible endpoint
  api_key: "..."
```

That works with OpenAI, OpenRouter, LiteLLM, Ollama, llama.cpp or vLLM, anything
speaking the OpenAI chat completions shape. `config.yaml` is gitignored. To keep
the key out of files entirely, use `api_key_env: MY_VAR` instead.

The capabilities ship asking for `model: qwen3.8-27b`, which is what this was
built against. If your backend does not serve that, map it in `config.yaml`
rather than editing the capability files:

```yaml
models:
  qwen3.8-27b: "gpt-4o-mini"        # or anthropic/claude-sonnet-4.5, llama3.1:8b, ...
```

That is the indirection the whole repo is about, so it is worth using rather
than editing four capability files by hand. A developer never sees either side
of it.

Leave `config.yaml` out and everything still runs against canned responses in
`fixtures/`, which is enough to watch every component work.

### 2. Set it up

```bash
./setup.sh
```

Safe to re-run. It ends with `ready`, or tells you exactly what broke.

### 3. Use it

```
http://<host>:8088/playground     pick a model, send anything
http://<host>:8088/assistant      ask for an outcome
```

Or from a terminal:

```bash
./capability list                   # the catalogue, and who owns each one
./capability run code-review        # ask for it, and notice you never name a model
./capability run deploy-to-prod     # nothing covers this yet
./capability scenarios              # the refusals, one at a time
./capability reset                  # back to the start, between runs
./capability verify                 # the self-test: 14 checks, 18 with a cluster
```

And the before picture, which is a plain shell script worth reading:

```bash
export LLM_GATEWAY=... LLM_API_KEY=...
cat before/review.sh
./before/review.sh
```

### What you need

| | For | Notes |
|---|---|---|
| Python 3.11+ | everything | `setup.sh` finds it |
| **Kyverno CLI** | component 4 | `brew install kyverno`, or the [releases page](https://github.com/kyverno/kyverno/releases). Tested against 1.19.2. JSON mode has no engine SDK yet, so this shells out to the CLI |
| `jq` | `before/review.sh` only | |
| A Kubernetes cluster | optional: the admission floor and the trace view | kind or k3s, with Kyverno installed |
| An OpenAI compatible endpoint | optional: real answers instead of canned ones | |

### Environment variables

Everything is configured in `config.yaml`. Three variables override it, for the
cases a file cannot cover:

| | |
|---|---|
| `PROMPT_LAYER_BACKEND` | `fixture` or `live`, overriding what `config.yaml` implies |
| `KUBECTL` | how to reach the cluster, if plain `kubectl` cannot. A root-only k3s kubeconfig wants `KUBECTL="sudo -n k3s kubectl"` |
| `PROMPT_LAYER_CONSOLE_SPANS` | print spans to the console when no OTLP endpoint is set |

## Troubleshooting

**`capability verify` fails.** It names the check that failed. Start there. It
covers the capability files, the allowlist, budgets, both policy modes and the
spans. Fourteen checks run anywhere; four more need a cluster with Kyverno and
are reported as `SKIP` without one.

**`the kyverno CLI is not on PATH`.** Component 4 shells out to it. Install it,
or the boundary check cannot run.

**Every answer looks canned.** `config.yaml` is missing or `base_url` is empty.
That is the intended fallback, not a failure.

**Answers are slow, or come back empty.** Reasoning models spend the token budget
thinking before they answer. Set `provider.thinking: false` in `config.yaml` to
send a flag that turns that off where the backend supports it. Measured on a
local 27B: 2.9s with it off, 22.9s with it on, and the same findings either way.

**A follow-up gets "no capability covers that".** Routing uses the `match:` list
in each capability file. A follow-up with no match stays with the capability
already answering, so a genuinely new subject needs **New chat**.

**The trace view is empty.** `observability.otlp_endpoint` is unset, or Jaeger is
not running. `capability reset` restarts Jaeger to clear old traces, so reload the
page afterwards. It stays empty until the next request.

**The admission policy refuses nothing.** Check Kyverno is installed
(`kubectl get crd clusterpolicies.kyverno.io`) and that you are acting as the
agent's service account. A `Forbidden` error means RBAC stopped it and the policy
never ran, which is a different thing. `verify` tells them apart.

**A capability edit does nothing.** Restart the web tools: the catalogue is
read at request time but the page is not.

**`before/review.sh` does not scale anything.** On purpose. Its last act is a
`kubectl scale` with no decision behind it, which is the point being made, so
it prints the command and stops. `RUN_IT=yes` runs it for real, against
whichever cluster your kubeconfig points at.

## Known quirks

Only `code-review` is wired end to end. The other capabilities exist so the
catalogue reads like a catalogue. `evals/` has structure but no suite that gates
anything. `on_limit: queue` reports the decision without a queue behind it. The
agent's tool call is chosen by scenario rather than parsed out of the model's
reply, because local models emit tool calls inconsistently and this component is
about the decision, not the parsing.

Spend is recorded from whatever the backend reports as a cost, and most
OpenAI-compatible backends report none, so a live run moves the ledger by
nothing. `capability run code-review --scenario over-budget` forces the
over-budget branch, and `capability reset` leaves `summarise-incident` sitting
at its limit, so the decision is reachable without waiting to spend $400.

The two web tools share one conversation each, with no authentication and no
per-viewer state. They are a demonstration, not a service.

Every `gen_ai.*` attribute is still `Development`. In semantic conventions
v1.42.0 (June 2026) the GenAI conventions were deprecated in the main repo and
moved to `open-telemetry/semantic-conventions-genai`, which has published no
tagged release.

## Licence

MIT. The owners, budgets and secrets in here are fictional.
