#!/bin/bash
# 打包「千问办公悬浮钮.app」—— 常驻千问办公窗口右下角的切换按钮
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="千问办公悬浮钮"
APP="$HOME/Applications/$APP_NAME.app"
BUNDLE_ID="com.local.qwenwork-switcher.fab"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key>      <string>launcher</string>
    <key>CFBundleIconFile</key>        <string>icon</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleShortVersionString</key> <string>1.1</string>
    <key>CFBundleVersion</key>         <string>2</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

cp "$SRC_DIR/fab.py" "$SRC_DIR/switcher.py" "$APP/Contents/Resources/app/"

cat > "$APP/Contents/MacOS/launcher" <<'LAUNCH'
#!/bin/bash
LOG="$HOME/.qwen-work-switcher/fab-launch.log"
mkdir -p "$HOME/.qwen-work-switcher"
exec >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') launch fab ==="

HERE="$(cd "$(dirname "$0")/../Resources/app" && pwd)"

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
      echo "$cand"; return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "ERROR: 没有找到 Tk 8.6+ 的 Python"
  osascript -e 'display alert "找不到可用的 Python（需要 Tk 8.6 以上）"' || true
  exit 1
fi

# 用 PID 文件防重复（不用 pgrep -f，命令行里含同样字符串的进程会被误判）
PIDFILE="$HOME/.qwen-work-switcher/fab.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "已有实例 pid=$(cat "$PIDFILE")，本次不重复启动"
  exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

echo "python: $PY"
exec "$PY" "$HERE/fab.py"
LAUNCH

chmod +x "$APP/Contents/MacOS/launcher"

ICON_SRC="/Applications/Agent/QwenWorkCN.app/Contents/Resources/icon.icns"
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$APP/Contents/Resources/icon.icns"
fi

touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

echo "✓ 已生成：$APP"
echo "  双击运行；右键按钮可退出、关闭跟随，或打开完整窗口。"
echo "  日志：~/.qwen-work-switcher/fab-launch.log"
