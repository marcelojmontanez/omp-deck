#!/bin/sh
set -eu

if ! command -v python3 >/dev/null 2>&1; then
  echo "OMP Deck requires Python 3.10 or newer." >&2
  exit 1
fi

exec python3 "$(dirname "$0")/omp_deck.py" install
