#!/usr/bin/env bash
#
# Live proof for the end to end connector's identity (ADR 0176 decision 4,
# #3243) against a real Kubernetes API server (1.30 or newer).
#
#   CURIE_E2E_IDENTITY_CONTEXT=<kube context> bash \
#     charts/curie/ci/runtime/e2e-connector-identity-runtime.sh
#
# It renders ONLY templates/e2e-connector-identity.yaml under a unique release
# name, applies it, and then acts as the connector's service account through
# impersonation, so every request is authorized and admitted by the API server
# exactly as the connector's own token would be. Everything it creates is named
# from RUN_ID and deleted on exit, including the cluster scoped objects.
#
# Allowed, and must succeed:
#   - create a prefixed, labelled namespace; bind the worker role inside it;
#     write and read a ConfigMap there; delete the namespace.
# Denied, and must be refused by the API server:
#   1. an unprefixed namespace (labelled);
#   2. a prefixed namespace without the ownership label;
#   3. a write in a namespace the identity did not create (prefixed and
#      labelled by an admin, so only the missing RoleBinding refuses it), and
#      a read there;
#   4. a cluster scoped create (a ClusterRole, a PriorityClass, a CRD);
#   5. deleting a namespace it did not create;
#   6. relabelling a namespace into its scope;
#   7. binding the worker role to another subject inside its own namespace.
set -euo pipefail

CTX="${CURIE_E2E_IDENTITY_CONTEXT:?set CURIE_E2E_IDENTITY_CONTEXT to the test cluster context}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CHART="$REPO_ROOT/charts/curie"
RUN_ID="${RUN_ID:-$(date +%s)}"
REL="e2eid${RUN_ID}"
NS="test-3243-e2e-identity-${RUN_ID}"
PREFIX="e2eid${RUN_ID}-"
LABEL_KEY="curietech.ai/e2e-owner"
LABEL_VALUE="$REL"
SA="${REL}-curie-e2e-connector"
WORKER="${REL}-curie-e2e-connector-namespace"
OWNED="${PREFIX}owned"
FOREIGN="${PREFIX}foreign"
OTHER="test-3243-other-${RUN_ID}"

k() { kubectl --context "$CTX" "$@"; }
as_sa() {
  kubectl --context "$CTX" --as "system:serviceaccount:${NS}:${SA}" \
    --as-group system:serviceaccounts --as-group "system:serviceaccounts:${NS}" "$@"
}

MANIFEST="$(mktemp)"
cleanup() {
  set +e
  for ns in "$OWNED" "$FOREIGN" "$OTHER" "${PREFIX}nolabel" "unprefixed-${RUN_ID}"; do
    k delete namespace "$ns" --ignore-not-found --wait=false >/dev/null 2>&1
  done
  k delete -f "$MANIFEST" --ignore-not-found --wait=false >/dev/null 2>&1
  k delete clusterrole "${PREFIX}probe" priorityclass "${PREFIX}probe" --ignore-not-found >/dev/null 2>&1
  k delete crd "probes.${PREFIX%?}.example.com" --ignore-not-found >/dev/null 2>&1
  k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1
  rm -f "$MANIFEST"
}
trap cleanup EXIT INT TERM

pass=0
ok() { echo "PASS $1"; pass=$((pass + 1)); }
fail() { echo "FAIL $1" >&2; exit 1; }

# expect_denied NAME CMD...: the command must fail, and with a refusal from
# the API server (RBAC Forbidden or the admission policy), not a typo.
expect_denied() {
  local name="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then
    fail "$name: was allowed: $out"
  fi
  if ! grep -Eq "Forbidden|forbidden|denied request|ValidatingAdmissionPolicy" <<<"$out"; then
    fail "$name: failed for another reason: $out"
  fi
  echo "PASS $name refused: $(head -c 300 <<<"$out" | tr '\n' ' ')"
  pass=$((pass + 1))
}

ns_yaml() { # name label-or-empty
  if [[ -n "$2" ]]; then
    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n  labels:\n    %s: %s\n' "$1" "$LABEL_KEY" "$2"
  else
    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$1"
  fi
}

k create namespace "$NS" >/dev/null
helm template "$REL" "$CHART" -n "$NS" \
  --set e2eConnectorIdentity.enabled=true \
  --set e2eConnectorIdentity.namespacePrefix="$PREFIX" \
  -s templates/e2e-connector-identity.yaml >"$MANIFEST"
k apply -f "$MANIFEST" >/dev/null
# A new policy takes a moment to reach the admission plugin. Wait until the
# identity's first refusal is the policy's, not a stale admit.
for _ in $(seq 1 30); do
  if ns_yaml "unprefixed-${RUN_ID}" "$LABEL_VALUE" | as_sa create -f - 2>&1 | grep -q "ValidatingAdmissionPolicy"; then
    break
  fi
  k delete namespace "unprefixed-${RUN_ID}" --ignore-not-found >/dev/null 2>&1
  sleep 2
done

# --- allowed path ---------------------------------------------------------
ns_yaml "$OWNED" "$LABEL_VALUE" | as_sa create -f - >/dev/null || fail "create owned namespace"
ok "create prefixed labelled namespace"
as_sa create rolebinding connector -n "$OWNED" --clusterrole "$WORKER" \
  --serviceaccount "${NS}:${SA}" >/dev/null || fail "bootstrap RoleBinding"
ok "bind worker role to itself in owned namespace"
as_sa create configmap probe -n "$OWNED" --from-literal=a=b >/dev/null || fail "write in owned namespace"
as_sa get configmap probe -n "$OWNED" >/dev/null || fail "read in owned namespace"
ok "read and write inside owned namespace"

# --- denials --------------------------------------------------------------
sa_create_ns() { ns_yaml "$1" "$2" | as_sa create -f -; }
expect_denied "1 unprefixed namespace" sa_create_ns "unprefixed-${RUN_ID}" "$LABEL_VALUE"
expect_denied "2 unlabelled namespace" sa_create_ns "${PREFIX}nolabel" ""

ns_yaml "$FOREIGN" "$LABEL_VALUE" | k create -f - >/dev/null
k create namespace "$OTHER" >/dev/null
expect_denied "3a write in a prefixed labelled namespace it did not create" \
  as_sa create configmap probe -n "$FOREIGN" --from-literal=a=b
expect_denied "3b write in an unrelated namespace" \
  as_sa create configmap probe -n "$OTHER" --from-literal=a=b
expect_denied "3c read in a namespace it did not create" as_sa get configmaps -n "$OTHER"
expect_denied "3d bootstrap RoleBinding in an unrelated namespace" \
  as_sa create rolebinding connector -n "$OTHER" --clusterrole "$WORKER" --serviceaccount "${NS}:${SA}"

expect_denied "4a cluster scoped create: ClusterRole" as_sa create clusterrole "${PREFIX}probe" --verb=get --resource=pods
expect_denied "4b cluster scoped create: PriorityClass" as_sa create priorityclass "${PREFIX}probe" --value=1
CRD_GROUP="${PREFIX%?}.example.com"
sa_create_crd() {
  as_sa create -f - <<EOF
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: probes.${CRD_GROUP}
spec:
  group: ${CRD_GROUP}
  names: {kind: Probe, plural: probes}
  scope: Namespaced
  versions: [{name: v1, served: true, storage: true, schema: {openAPIV3Schema: {type: object}}}]
EOF
}
expect_denied "4c cluster scoped create: CRD" sa_create_crd
expect_denied "4d cluster admin binding" as_sa create clusterrolebinding "${PREFIX}probe" --clusterrole cluster-admin --serviceaccount "${NS}:${SA}"

expect_denied "5 delete a namespace it did not create" as_sa delete namespace "$OTHER" --wait=false
expect_denied "6 relabel a namespace into its scope" as_sa label namespace "$OTHER" "${LABEL_KEY}=${LABEL_VALUE}"
expect_denied "7 hand the worker role to another subject" \
  as_sa create rolebinding leak -n "$OWNED" --clusterrole "$WORKER" --serviceaccount "${NS}:default"

as_sa delete namespace "$OWNED" --wait=false >/dev/null || fail "delete owned namespace"
ok "delete owned namespace"

echo "ALL PASS ($pass checks) context=$CTX run=$RUN_ID"
