#!/bin/bash
# 用「带 Tk 8.6+ 的 Python」启动图形界面
# 为什么：macOS 自带的 /usr/bin/python3 只带 Tk 8.5，在较新的 macOS 上
#         窗口会白屏 / 内容不绘制，必须挑一个 Tk 较新的解释器。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pick_python() {
  local cand
  for cand in \
    "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3 \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/*/bin/python3 \
    /usr/bin/python3 ; do
    [ -x "$cand" ] || continue
    if "$cand" -c 'import tkinter, sys; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)' 2>/dev/null; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  osascript -e 'display alert "找不到可用的 Python（需要 Tk 8.6 以上）" message "请安装 python.org 官方版或 Homebrew 的 python-tk 后重试。"' 2>/dev/null || true
  echo "未找到带 Tk 8.6+ 的 Python" >&2
  exit 1
fi

exec "$PY" "$DIR/fab.py"
