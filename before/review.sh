#!/usr/bin/env bash
# AI code review, the way it gets done today. Forty lines, no layer at all.
#
# Worth reading rather than running: every decision this script makes is one
# a developer made alone, in a file nobody reviews. The model name, what gets
# attached, what it may do afterwards.
#
#   export LLM_GATEWAY=https://your-endpoint/v1
#   export LLM_API_KEY=...
#   ./before/review.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

: "${LLM_GATEWAY:?set LLM_GATEWAY to an OpenAI-compatible endpoint, including /v1}"
: "${LLM_API_KEY:?set LLM_API_KEY to the key for that endpoint}"
command -v jq >/dev/null || { echo "this script needs jq"; exit 1; }

# Hardcoded, because there was nowhere else to put it. Picked six months ago,
# when it worked. Whether your backend still serves it is now this script's
# problem, and the developer's afternoon.
MODEL="${MODEL:-claude-3-5-sonnet-20241022}"
CONTEXT="$(cat context/*)"                      # whatever is in the folder

echo "model     $MODEL"
echo "context   $(ls context/ | tr '\n' ' ')"
echo "budget    -"
echo "owner     -"
echo

read -r -d '' PROMPT <<PROMPT_END || true
Review this change. Report defects that change behaviour in production.
At most 3 findings, one line each as 'path:line - what breaks'. No preamble.
Then, if the service is unsafe, give the kubectl command that stops traffic.

$CONTEXT
PROMPT_END

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

jq -n --arg model "$MODEL" --arg prompt "$PROMPT" '{
  model:    $model,
  messages: [ { role: "user", content: $prompt } ],
  max_tokens: 900,
  chat_template_kwargs: { enable_thinking: false }
}' | curl -sS "$LLM_GATEWAY/chat/completions" \
       -H "Authorization: Bearer $LLM_API_KEY" \
       -H 'Content-Type: application/json' \
       --data-binary @- > "$OUT"

if jq -e .error "$OUT" >/dev/null 2>&1; then
  echo "FAILED    $(jq -r '.error.message // "the gateway returned an error"' "$OUT")"
  exit 7
fi

jq -r '.choices[0].message.content // "(the model returned nothing)"' "$OUT"
echo
echo "tokens    $(jq -r '.usage.prompt_tokens // 0' "$OUT")+$(jq -r '.usage.completion_tokens // 0' "$OUT"), billed to nobody"

# And here is the part with no decision behind it at all. The review said the
# service is unsafe, the script has admin, so it acts. Nothing asked whether
# this script may scale a production workload, and nothing recorded that it did.
#
# Which is why it is OPT-IN here and not in the demo it is contrasted with:
# a repo you cloned should not resize a deployment in whatever cluster your
# kubeconfig happens to point at.
echo
echo "\$ kubectl -n demo-prod scale deploy/checkout --replicas=0"
if [[ "${RUN_IT:-no}" == "yes" ]]; then
  ${KUBECTL:-kubectl} -n demo-prod scale deploy/checkout --replicas=0
else
  echo "          not run. RUN_IT=yes, against a cluster you do not mind, to really do it."
fi
