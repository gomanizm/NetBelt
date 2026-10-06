#!/bin/bash
# Claude Code のクラウドセッション（Linux のコンテナ）で、テストを走らせる
# 準備をする。手元（Windows）では何もしない。
#
# CI（.github/workflows/tests.yml）に合わせて Python 3.11 の venv を作り、
# requirements.txt と pytest を入れる。venv はリポジトリの外に置く
# （.gitignore は .venv/ を除外していない）。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

VENV="${HOME}/.venvs/netbelt"

# PyQt6 は offscreen でも libEGL.so.1 を要る（無いと import で落ちる）。
# コンテナのイメージには入っていない。
if ! ldconfig -p | grep -q 'libEGL\.so\.1'; then
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
  fi
  $SUDO apt-get update -q
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -q --no-install-recommends libegl1
fi

if [ ! -x "${VENV}/bin/python" ]; then
  python3.11 -m venv "${VENV}"
fi

# pytest の版は CI と同じものにする。CI 側で上げたら追従する。
PYTEST_PIN="$(grep -oE 'pytest==[0-9][0-9A-Za-z.]*' .github/workflows/tests.yml | head -n 1 || true)"

"${VENV}/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
"${VENV}/bin/python" -m pip install -q --disable-pip-version-check \
  -r requirements.txt "${PYTEST_PIN:-pytest}"

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"${VENV}\""
    echo "export PATH=\"${VENV}/bin:\${PATH}\""
    echo "export QT_QPA_PLATFORM=offscreen"
  } >> "${CLAUDE_ENV_FILE}"
fi
