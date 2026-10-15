#!/usr/bin/env bash
# Runs on the server. The CI deploy key's forced command does `git pull` and
# then execs this script, so it always runs the just-pulled version.
set -euo pipefail
cd "$(dirname "$0")"

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
