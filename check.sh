#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-all}" in
    lint)
        uv run ruff format .
        uv run ruff check --fix .
        uv run mypy
        ;;
    test)
        uv run pytest "${@:2}"
        ;;
    all)
        uv run ruff format --check .
        uv run ruff check .
        uv run mypy
        uv run pytest
        ;;
    *)
        echo "usage: $0 [lint|test|all]"
        exit 1
        ;;
esac
