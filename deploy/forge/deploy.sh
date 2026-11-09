#!/usr/bin/env bash
# Build, push and deploy Avenza to the Forge 2026 sandbox.
#
#   export KUBECONFIG=~/forge/teamXX.yaml
#   deploy/forge/deploy.sh team07 ghcr.io/<you>/avenza v3
#
# Follows the guide: linux/amd64 image (§4.1), tagged releases, the reference
# manifest shape (§4.2) and the "app" Service on port 80 (§4.3).
set -euo pipefail

TEAM="${1:?usage: deploy.sh <teamXX> <registry/image> <tag>}"
IMAGE="${2:?usage: deploy.sh <teamXX> <registry/image> <tag>}"
TAG="${3:?usage: deploy.sh <teamXX> <registry/image> <tag> (never 'latest')}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if [[ "$TAG" == "latest" ]]; then
  echo "Use a version tag (v1, v2 or a git SHA), not 'latest' (guide §4.4)." >&2
  exit 1
fi

echo "==> Checking kubectl access for $TEAM"
kubectl auth can-i create deployments >/dev/null || { echo "kubectl cannot create deployments; check KUBECONFIG" >&2; exit 1; }
kubectl get secret huawei-gateway >/dev/null 2>&1 || echo "warning: secret huawei-gateway not found; bank and MaaS calls will fail" >&2

echo "==> Building $IMAGE:$TAG for linux/amd64"
docker build --platform linux/amd64 -t "$IMAGE:$TAG" "$ROOT"
docker push "$IMAGE:$TAG"

if [[ -f "$HERE/.env.forge" ]]; then
  echo "==> Updating secret avenza-secrets from deploy/forge/.env.forge"
  kubectl create secret generic avenza-secrets --from-env-file="$HERE/.env.forge" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "warning: deploy/forge/.env.forge not found; speech keys and ADMIN_TOKEN will be missing" >&2
fi

echo "==> Applying manifest"
sed -e "s#teamXX#${TEAM}#g" -e "s#IMAGE#${IMAGE}:${TAG}#g" "$HERE/app.yaml" | kubectl apply -f -
kubectl rollout restart deployment/app >/dev/null 2>&1 || true
kubectl rollout status deployment/app --timeout=180s

echo "==> Verifying https://${TEAM}.xtradot.co"
curl -sS -o /dev/null -w "%{http_code}\n" "https://${TEAM}.xtradot.co/healthz"
echo "Done. Next, once: register the bank webhook (see docs/FORGE.md)."
