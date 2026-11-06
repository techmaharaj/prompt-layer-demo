#!/usr/bin/env bash
# One command to get from a fresh clone to a working demo. Safe to re-run.
#
#   ./setup.sh
#   ./capability list
#
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; D=$'\033[2m'; O=$'\033[0m'
ok()   { echo "  ${G}ok${O}    $*"; }
warn() { echo "  ${Y}warn${O}  $*"; }
die()  { echo "  ${R}fail${O}  $*" >&2; exit 1; }

echo
echo "${B}prompt-layer-demo setup${O}"
echo

# --- 1. python ------------------------------------------------------------
PYBIN=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
    major=${v%%.*}; minor=${v##*.}
    if (( major == 3 && minor >= 11 )); then PYBIN=$c; break; fi
  fi
done
[[ -n $PYBIN ]] || die "need python 3.11 or newer; none found on PATH"
ok "python: $PYBIN ($($PYBIN -V 2>&1 | awk '{print $2}'))"

# --- 2. venv --------------------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
  rm -rf .venv
  $PYBIN -m venv .venv 2>/dev/null || die "could not create .venv. On Debian/Ubuntu: sudo apt install ${PYBIN}-venv"
  ok "created .venv"
else
  ok ".venv already there"
fi

# --- 3. deps --------------------------------------------------------------
.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
if .venv/bin/python -m pip install -q -e . >/dev/null 2>&1; then
  ok "installed deps + the \`capability\` command"
else
  .venv/bin/python -m pip install -q -r requirements.txt || die "pip install failed"
  warn "editable install failed; deps are in, use ./capability"
fi

# --- 4. kyverno (component 4 is evaluated for real) -----------------------
if command -v kyverno >/dev/null 2>&1; then
  ok "kyverno: $(kyverno version 2>/dev/null | awk '/^Version/{print $2}')  ${D}(verified against 1.19.2)${O}"
else
  warn "kyverno CLI not found: component 4 (agent permissions) cannot be evaluated."
  echo "        macOS:  brew install kyverno"
  echo "        linux:  https://github.com/kyverno/kyverno/releases  (kyverno-cli_<v>_linux_x86_64.tar.gz)"
fi

# --- 5. the cluster half, if there is a cluster ---------------------------
# Optional by design: everything except the admission floor and the trace view
# works without Kubernetes at all.
KUBECTL=${KUBECTL:-}
if [[ -z $KUBECTL ]]; then
  if command -v kubectl >/dev/null 2>&1 && kubectl version --request-timeout=3s >/dev/null 2>&1; then
    KUBECTL="kubectl"
  elif command -v k3s >/dev/null 2>&1 && sudo k3s kubectl version --request-timeout=3s >/dev/null 2>&1; then
    KUBECTL="sudo k3s kubectl"
  fi
fi
if [[ -n $KUBECTL ]]; then
  if $KUBECTL get crd clusterpolicies.kyverno.io >/dev/null 2>&1; then
    $KUBECTL apply -f k8s/demo-prod.yaml >/dev/null 2>&1 \
      && $KUBECTL apply -f policy/admission.yaml >/dev/null 2>&1 \
      && ok "cluster: demo-prod workload + admission policy applied"
    $KUBECTL apply -f k8s/jaeger.yaml >/dev/null 2>&1 \
      && ok "cluster: Jaeger on :30686 (UI) and :30318 (OTLP in)"
  else
    warn "cluster reachable but Kyverno is not installed: the admission floor is skipped."
    echo "        helm repo add kyverno https://kyverno.github.io/kyverno/ && \\"
    echo "        helm install kyverno kyverno/kyverno -n kyverno --create-namespace"
  fi
else
  warn "no cluster reachable: admission floor and trace view skipped (everything else works)."
fi

# --- 6. the developer's page, as a service --------------------------------
# A page you have to remember to start is a page that is down when you need it.
if command -v systemctl >/dev/null 2>&1 && [[ -d /etc/systemd/system ]] && sudo -n true 2>/dev/null; then
  # @DIR@ becomes wherever this clone actually lives, so the unit works outside
  # the one directory it was first written for.
  sed "s#@DIR@#$PWD#g" scripts/review-page.service \
    | sudo install -m 644 /dev/stdin /etc/systemd/system/review-page.service
  sudo systemctl daemon-reload
  sudo systemctl enable -q --now review-page.service
  for _ in $(seq 20); do
    curl -fsS -m 2 http://127.0.0.1:8088/healthz >/dev/null 2>&1 && break; sleep 1
  done
  if curl -fsS -m 2 http://127.0.0.1:8088/healthz >/dev/null 2>&1; then
    ok "developer page: review-page.service on :8088  ${D}/playground and /assistant${O}"
  else
    warn "review-page.service did not come up: sudo journalctl -u review-page -n 20"
  fi
else
  warn "no systemd (or no passwordless sudo): run the page yourself: .venv/bin/python -m layer.web 8088"
fi

# --- 7. prove it works ----------------------------------------------------
echo
echo "${B}self-test${O}"
.venv/bin/python -m scripts.selftest \
  || die "self-test failed, see above. Nothing is half-working silently."

cat <<BANNER

${B}ready.${O} Try these, in this order:

  ./capability reset             ${D}the starting point${O}
  ./capability list              ${D}the catalogue: four capabilities, each with an owner${O}
  ./capability run code-review   ${D}ask for it. Notice you never name a model.${O}
  ./capability scenarios         ${D}the refusals${O}
  ./capability verify            ${D}the self-test again, on its own${O}
  ./capability --help

${D}Prefer a bare \`capability\`?  source .venv/bin/activate${O}

BANNER
