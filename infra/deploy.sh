#!/usr/bin/env bash
#
# Build the image, push it, and apply the infrastructure.
#
# The ordering is not incidental. A container-image Lambda cannot be created
# before the image exists, and the image cannot be pushed before the registry
# exists, so the registry is applied on its own first. The Function URL's
# hostname is then only known after the function exists, and the function needs
# that hostname in ALLOWED_HOSTS, so the last apply feeds this configuration's
# own output back in as a variable — declarative, and nothing for a later apply
# to revert.
#
# Safe to re-run: every step converges.
#
#   ./deploy.sh           # build, push, apply
#   ./deploy.sh --seed    # ...and repopulate the business (destructive)
set -euo pipefail

cd "$(dirname "$0")"

SEED=false
[[ "${1:-}" == "--seed" ]] && SEED=true

# Tagging by commit makes the deployed version answerable from the console, and
# gives Lambda a genuinely new image_uri per build. Re-pushing one mutable tag
# would leave the function pinned to the digest it first resolved.
TAG="$(git rev-parse --short HEAD)"
[[ -n "$(git status --porcelain -- .. 2>/dev/null)" ]] && TAG="${TAG}-dirty"

echo "==> terraform init"
terraform init -input=false

echo "==> Registry first (the image has nowhere to go until it exists)"
terraform apply -input=false -auto-approve -target=aws_ecr_repository.this

REPO="$(terraform output -raw repository_url)"
REGISTRY="${REPO%%/*}"

echo "==> Building ${REPO}:${TAG}"
# --platform is load-bearing: the function is declared x86_64 and a Mac or any
# arm64 host would otherwise silently produce an image Lambda cannot run.
#
# --provenance and --sbom are load-bearing too, and less obviously. Recent
# BuildKit attaches provenance and SBOM attestations by default, and to carry
# them it must push an OCI *image index* rather than a plain manifest. Lambda
# only accepts a single Docker v2 manifest and rejects the index at
# CreateFunction with "The image manifest, config or layer media type for the
# source image ... is not supported" — a message that says nothing about
# attestations and sends you looking at the base image instead.
#
# The build and the local Runtime Interface Emulator run both succeed with the
# index, because the failure is in what the registry stores, not in the image.
docker build --platform linux/amd64 --provenance=false --sbom=false \
  -t "${REPO}:${TAG}" ..

echo "==> Pushing"
aws ecr get-login-password --region "$(terraform output -raw region 2>/dev/null || echo us-east-1)" \
  | docker login --username AWS --password-stdin "${REGISTRY}"
docker push "${REPO}:${TAG}"

echo "==> Applying the rest"
terraform apply -input=false -auto-approve -var "image_tag=${TAG}"

HOST="$(terraform output -raw function_url_host)"
echo "==> Second pass: ALLOWED_HOSTS=${HOST}"

# Recorded so a later bare `terraform apply` — to change a timeout, say — does
# not fall back to the empty default and take the server down with 421s. The
# hostname is not a secret; it is the public endpoint.
cat > terraform.tfvars <<EOF
# Written by deploy.sh. The Function URL's hostname, fed back in from this
# configuration's own output — see the allowed_hosts variable for why it cannot
# be a direct reference.
allowed_hosts = "${HOST}"
EOF

terraform apply -input=false -auto-approve \
  -var "image_tag=${TAG}" -var "allowed_hosts=${HOST}"

FUNCTION="$(terraform output -raw function_name)"

if [[ "$SEED" == true ]]; then
  echo "==> Seeding the table"
  # Over the CLI rather than the Function URL on purpose: seeding rewrites the
  # catalog and the whole booking calendar, and that is not an endpoint to leave
  # exposed on a public URL.
  aws lambda invoke \
    --function-name "$FUNCTION" \
    --cli-binary-format raw-in-base64-out \
    --payload '{"action":"seed","days":60}' \
    /dev/stdout >/dev/null
fi

ENDPOINT="$(terraform output -raw mcp_endpoint)"

echo "==> Smoke test: tools/list, three times"
# Three, because one proves nothing. Mangum re-runs the ASGI lifespan on every
# invocation and MCP's session manager refuses to start twice, so the failure
# this catches only appears from the second request into a warm container.
for i in 1 2 3; do
  code="$(curl -s -o /tmp/mcp-smoke.json -w '%{http_code}' -X POST "$ENDPOINT" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
  tools="$(python3 -c 'import json,sys; d=json.load(open("/tmp/mcp-smoke.json")); print(len(d.get("result",{}).get("tools",[])))' 2>/dev/null || echo '?')"
  echo "    request ${i}: HTTP ${code}, ${tools} tools"
  [[ "$code" == "200" ]] || { echo "FAILED"; cat /tmp/mcp-smoke.json; exit 1; }
done

# The URL in the README. It answered 502 in a browser for a while, and nothing
# failed — the protocol was fine and no check looked.
ROOT="$(curl -s -o /dev/null -w '%{http_code}' "$(terraform output -raw function_url)")"
echo "    GET /: HTTP ${ROOT}"
[[ "$ROOT" == "200" ]] || { echo "FAILED"; exit 1; }

echo
echo "MCP endpoint: ${ENDPOINT}"
