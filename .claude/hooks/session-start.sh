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
# コンテナのイメージには入っていない。フォントの 3 つは下で使う。
# 入っているかは dpkg に訊く。ldconfig -p | grep -q だと、grep が先に
# 終わったときの SIGPIPE を pipefail が拾い、入っていても「無い」になる。
PACKAGES=(libegl1 fonts-inconsolata fonts-liberation fontconfig)
MISSING=()
for pkg in "${PACKAGES[@]}"; do
  status="$(dpkg-query -W -f='${Status}' "${pkg}" 2> /dev/null || true)"
  if [ "${status}" != "install ok installed" ]; then
    MISSING+=("${pkg}")
  fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
  fi
  $SUDO apt-get update -q
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -q --no-install-recommends \
    "${MISSING[@]}"
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

# テストは Windows の既定のフォントを前提に、名前と幅を確かめる。
# Linux には無く、代わりのフォントで次のように落ちる（実測）。
# - Consolas（ターミナルの既定）が DejaVu Sans Mono になり、1 文字 8px
#   （Consolas は 7px）。既定の幅で 80 桁が 79 桁になる
#   （test_terminal_widget_render）
# - Courier New が Liberation Mono と名乗る（test_settings_dialog）
# - ヒンティングが無いと行の高さが小数になり、行数の計算（整数）とずれて
#   1 行目が画面の外へ出る（test_terminal_selection_copy ほか）
# 既定の 10pt で Consolas と同じ 7px になる Inconsolata（大きさによっては
# 1px ずれる）を Consolas、Courier New と同じ幅の Liberation Mono を
# Courier New という名前で見せ、ヒンティングを Windows に近づける。
# venv と PATH より後に置く（ここで落ちても、テストは走らせられる）。
FONT_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/fonts/netbelt-tests"
FONT_CONF_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/fontconfig/conf.d"
mkdir -p "${FONT_DIR}/consolas" "${FONT_DIR}/courier-new" "${FONT_CONF_DIR}"
# このフックは再開・/clear・圧縮でも走り、そのとき裏でテストの Qt が
# フォントを mmap していることがある。cp -f は同じファイルを切り詰めて
# 書き直すので、その Qt が SIGBUS で落ちる（実測）。install -C は中身が
# 同じなら触らず、違えば消してから作る。設定も別名に書いてから置き換える。
install -C -m 644 "$(dpkg -L fonts-inconsolata | grep -m 1 '/Inconsolata\.otf$')" \
  "${FONT_DIR}/consolas/"
install -C -m 644 $(dpkg -L fonts-liberation | grep '/LiberationMono-[A-Za-z]*\.ttf$') \
  "${FONT_DIR}/courier-new/"
FONT_CONF="${FONT_CONF_DIR}/60-netbelt-tests.conf"
cat > "${FONT_CONF}.tmp" << 'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<!-- .claude/hooks/session-start.sh が書く。手で直しても次の起動で戻る -->
<fontconfig>
  <match target="scan">
    <test name="file" compare="contains"><string>/netbelt-tests/consolas/</string></test>
    <edit name="family" mode="assign" binding="same"><string>Consolas</string></edit>
  </match>
  <match target="scan">
    <test name="file" compare="contains"><string>/netbelt-tests/courier-new/</string></test>
    <edit name="family" mode="assign" binding="same"><string>Courier New</string></edit>
  </match>
  <match target="font">
    <edit name="hinting" mode="assign"><bool>true</bool></edit>
    <edit name="hintstyle" mode="assign"><const>hintfull</const></edit>
  </match>
</fontconfig>
EOF
mv -f "${FONT_CONF}.tmp" "${FONT_CONF}"
fc-cache -f "${FONT_DIR}" > /dev/null
