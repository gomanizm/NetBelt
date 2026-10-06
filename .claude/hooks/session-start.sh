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

# python があるかだけで判断しない。venv の作成や pip の更新が途中で止まると、
# python はあって pip が無い venv が残り、以後の起動がすべて pip の行で落ちる。
# pip の有無だけでも足りない。pyvenv.cfg を書く前に止まった venv の python は
# システムの Python として動き、システムの pip を見つける（実測。そのまま
# 進むと、システムの pip を入れ替えようとして落ちる）。
if ! "${VENV}/bin/python" -c 'import sys, pip; sys.exit(sys.prefix == sys.base_prefix)' \
    > /dev/null 2>&1; then
  python3.11 -m venv --clear "${VENV}"
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
