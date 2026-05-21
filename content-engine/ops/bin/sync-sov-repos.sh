#!/usr/bin/env bash
set -euo pipefail

ROOT="${CLAW_ROOT:-/opt/claw}"

sync_repo() {
  local name="$1"
  local branch="$2"
  local url="$3"
  local dir="$ROOT/$name"

  echo "== $name ($branch) =="
  if [ ! -d "$dir/.git" ]; then
    if [ -e "$dir" ] && [ "$(find "$dir" -mindepth 1 -maxdepth 1 | wc -l)" -gt 0 ]; then
      echo "Refusing to clone over non-empty non-git directory: $dir" >&2
      return 2
    fi
    rm -rf "$dir"
    git clone --branch "$branch" "$url" "$dir"
  fi

  cd "$dir"
  if [ -n "$(git status --porcelain)" ]; then
    echo "Refusing to pull with local changes in $dir" >&2
    git status --short
    return 3
  fi

  git fetch origin "$branch"
  git checkout "$branch"
  git pull --ff-only origin "$branch"
  git rev-parse --short HEAD
}

sync_repo "clawbytes" "master" "https://github.com/SovereignSignal/clawbytes.git"
sync_repo "modelbytes" "master" "https://github.com/SovereignSignal/modelbytes.git"
sync_repo "claw-consulting-site" "main" "https://github.com/SovereignSignal/claw-consulting-site.git"

echo "All Sov repos are up to date."

