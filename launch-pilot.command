#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"
python=""
for candidate in "${DBP_PGL_PYTHON:-}" "$PWD/.venv/bin/python" "$HOME/.local/bin/python3.12" python3.12 python3; do
    if [ -n "$candidate" ] && "$candidate" -c 'import sys, tkinter; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        python="$candidate"
        break
    fi
done
if [ -z "$python" ]; then
    printf '%s\n' 'Python 3.12+ with Tk is required. See README.md for setup.'
    exit 1
fi
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" -m dbp_pgl_runner launch
