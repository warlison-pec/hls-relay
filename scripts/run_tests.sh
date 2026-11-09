#!/usr/bin/env bash
# Offline test suite (no access to the real origin). Needs python3 >= 3.10;
# the end-to-end test also needs ffmpeg (skipped otherwise).
# Usage: bash scripts/run_tests.sh [--quick]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -m py_compile relay.py
python3 -I tests/test_relay_v04.py -v
python3 -I tests/test_multichannel.py -v
python3 -I tests/test_slate.py -v SlateTests
if [ "${1:-}" != "--quick" ]; then
  python3 -I tests/test_slate.py -v SlateEndToEnd
  python3 -I tests/test_e2e_ffmpeg.py -v
fi
