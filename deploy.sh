#!/usr/bin/env bash
# Runs on the server: as the forced command of the CI deploy key, or by hand.
#   deploy.sh sha256:<digest>   pin that image digest in .env and deploy it
#   deploy.sh                   redeploy whatever .env currently pins
# CI passes the digest as the SSH command; sshd hands it to the forced command
# as SSH_ORIGINAL_COMMAND. Anything that is not a digest is refused.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=ghcr.io/ffoxin/telegrief
ref="${1:-${SSH_ORIGINAL_COMMAND:-}}"
if [ -n "$ref" ]; then
  if ! [[ "$ref" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "refusing to deploy '$ref': expected sha256:<64 hex digits>" >&2
    exit 2
  fi
  {
    [ -f .env ] && grep -v '^TELEGRIEF_IMAGE=' .env || true
    echo "TELEGRIEF_IMAGE=$IMAGE@$ref"
  } > .env.tmp
  mv .env.tmp .env
fi
echo "deploying $(grep '^TELEGRIEF_IMAGE=' .env 2>/dev/null || echo "$IMAGE:latest (nothing pinned)")"

docker compose pull --quiet
docker compose up --detach --remove-orphans
docker image prune --force >/dev/null

container=$(docker compose ps --quiet app)
status=starting
for _ in $(seq 1 30); do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$container")
  [ "$status" = healthy ] && break
  sleep 5
done
docker compose ps
if [ "$status" != healthy ]; then
  # No log dump here: this output lands in public CI logs.
  echo "app is $status after deploy; inspect with: docker compose logs app" >&2
  exit 1
fi
