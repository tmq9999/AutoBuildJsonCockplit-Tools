#!/usr/bin/env bash
set -Eeuo pipefail

# Start the private OAuth workbench from any working directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
PORT="${OAUTH_PORT:-8787}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Không tìm thấy Python virtualenv: ${PYTHON_BIN}" >&2
    echo "Hãy tạo/cài .venv trước khi chạy server OAuth." >&2
    exit 1
fi

if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "OAUTH_PORT không hợp lệ: ${PORT}" >&2
    exit 1
fi

# Avoid starting a second coordinator against the same local data directory.
if "${PYTHON_BIN}" - "${PORT}" <<'PY' >/dev/null 2>&1
import sys
from urllib.request import urlopen

port = sys.argv[1]
try:
    with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
        page = response.read(8192).decode("utf-8", "ignore")
except Exception:
    raise SystemExit(1)

raise SystemExit(0 if "OAUTH WORKBENCH" in page else 1)
PY
then
    echo "OAuth server đang chạy: http://127.0.0.1:${PORT}"
    exit 0
fi

cd -- "${PROJECT_ROOT}"
echo "Khởi động OAuth server tại http://127.0.0.1:${PORT}"
echo "Nhấn Ctrl+C để dừng server."
exec env AUTOBUILD_HOST=127.0.0.1 "${PYTHON_BIN}" -m autobuild_json --port "${PORT}"
