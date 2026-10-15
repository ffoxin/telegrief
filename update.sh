#!/usr/bin/env bash

# LICENSE:
# MIT License
#
# Copyright (c) 2025 Vital Kolas
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# INFO:
# This script in current docker compose setup:
# - rebuilds necessary images using latest image versions
# - updates base images with latest image versions
# - restarts everything
# - shows the current status
# - stops on any error

# INSTALLATION:
# wget -O update.sh 'https://gist.githubusercontent.com/ffoxin/338ccd4ec811d96d7d557a7eac108077/raw/update.sh' && chmod +x update.sh

# USAGE EXAMPLES:
#   ./update.sh                    # Updates all containers
#   ./update.sh web_service db     # Updates only web_service and db containers

# self-update
curl --max-time 5 --silent --output "update.sh" --time-cond "update.sh" "https://gist.githubusercontent.com/ffoxin/338ccd4ec811d96d7d557a7eac108077/raw/update.sh"

CONTAINERS=("$@")

if [ ${#CONTAINERS[@]} -eq 0 ]; then
    # update everything
    docker compose build --pull && \
    docker compose pull && \
    docker compose down && \
    docker compose up --detach --remove-orphans && \
    docker image prune --force && \
    docker network prune --force && \
    docker compose ps
else
    # update only specified containers
    docker compose build --pull "${CONTAINERS[@]}" && \
    docker compose pull "${CONTAINERS[@]}" && \
    docker compose stop "${CONTAINERS[@]}" && \
    docker compose rm -f "${CONTAINERS[@]}" && \
    docker compose up --detach "${CONTAINERS[@]}" && \
    docker image prune --force && \
    docker network prune --force && \
    docker compose ps "${CONTAINERS[@]}"
fi
