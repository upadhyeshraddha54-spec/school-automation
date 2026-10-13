#!/usr/bin/env bash
# Start the Fees Calling Desk on a Mac with SYNTHETIC test data.
# Safe to run again: it only creates what is missing.
set -euo pipefail
cd "$(dirname "$0")"

# Homebrew's PostgreSQL is "keg-only"; make its tools visible.
for d in /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/postgresql@16/bin; do
  [ -d "$d" ] && export PATH="$d:$PATH"
done

need() { echo; echo "⚠️  $1"; exit 1; }
command -v brew >/dev/null || need "Homebrew is missing. Install it from https://brew.sh and run this script again."
command -v psql >/dev/null || need "PostgreSQL is missing. Run:  brew install postgresql@16 && brew services start postgresql@16"
pg_isready -q || { brew services start postgresql@16 >/dev/null 2>&1 || true; sleep 3; }
pg_isready -q || need "PostgreSQL is not running. Run:  brew services start postgresql@16"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || need "Python 3.11 or newer is needed. Run:  brew install python@3.12"

# Database (uses your Mac user, no password, local only)
psql -lqt | cut -d '|' -f1 | grep -qw schoolops || createdb -E UTF8 schoolops

# Python packages
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Synthetic test school (fake students only)
[ -d synthetic_school ] || .venv/bin/python -m schoolops_sync make-synthetic synthetic_school >/dev/null

# Local test settings
if [ ! -f local.toml ]; then
  sed -e "s#^watch_folder = .*#watch_folder = \"$(pwd)/synthetic_school\"#" \
      -e "s#^database_url = .*#database_url = \"postgresql:///schoolops\"#" \
      -e "s#^dry_run = .*#dry_run = true#" \
      tests/test_config.toml > local.toml
  printf '\n# Mac test settings\n' >> local.toml
fi

.venv/bin/python -m schoolops_sync --config local.toml init-db
echo
echo "✅ Fees Calling Desk is starting with SYNTHETIC students: http://127.0.0.1:8000"
echo "   Students & Fees → “+ Add Excel” → choose the files in: $(pwd)/synthetic_school"
echo "   Press Ctrl+C to stop."
( sleep 2; open "http://127.0.0.1:8000" ) >/dev/null 2>&1 &
exec .venv/bin/python -m schoolops_sync --config local.toml serve --no-watch
