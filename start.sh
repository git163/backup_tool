#!/bin/bash
# Start the backup and patch GUI tool

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "Error: python or python3 not found"
    exit 1
fi

export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH}"
$PYTHON gui/main_window.py "$@"
