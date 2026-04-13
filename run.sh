#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
~/proposal-ai-venv/bin/python "$SCRIPT_DIR/main.py" "$@"
