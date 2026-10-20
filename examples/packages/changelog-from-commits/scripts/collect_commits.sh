#!/bin/sh
set -eu

range="${1:-}"

if [ -z "$range" ]; then
  last_tag="$(git describe --tags --abbrev=0 2>/dev/null || true)"
  if [ -n "$last_tag" ]; then
    range="$last_tag..HEAD"
  else
    range="HEAD"
  fi
fi

git log --no-merges --format='%H%x1f%s%x1f%b%x1e' "$range"
