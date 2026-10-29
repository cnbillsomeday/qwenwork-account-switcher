#!/bin/bash
# 把图形界面打包成可双击的 .app（放在 ~/Applications 下）
# 注意：.app 必须自包含 —— macOS 不允许由 launchd 启动的 app 去执行
#      ~/Documents 里的脚本（会报 Operation not permitted）。
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="千问办公切换器"
APP="$HOME/Applications/$APP_NAME.app"
BUNDLE_ID="com.local.qwenwork-switcher"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

# ── Info.plist ───────────────────────────────────────────────────────
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
    <key>CFBundleShortVersionString</key> <string>1.0</string>
    <key>CFBundleVersion</key>         <string>1</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

# ── 自包含：把代码带进 app 内部 ──────────────────────────────────────
cp "$SRC_DIR/gui.py" "$SRC_DIR/switcher.py" "$APP/Contents/Resources/app/"

# ── 启动器 ───────────────────────────────────────────────────────────
cat > "$APP/Contents/MacOS/launcher" <<'LAUNCH'
#!/bin/bash
# 由 launchd 启动时没有终端环境，输出统一落到日志
LOG="$HOME/.qwen-work-switcher/gui-launch.log"
mkdir -p "$HOME/.qwen-work-switcher"
exec >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') launch ==="

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
  osascript -e 'display alert "找不到可用的 Python（需要 Tk 8.6 以上）" message "安装 python.org 官方版或 Homebrew 的 python-tk 后重试。"' || true
  exit 1
fi

echo "python: $PY"
exec "$PY" "$HERE/gui.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"

# ── 图标：借用千问办公的，视觉上更有归属感 ───────────────────────────
ICON_SRC="/Applications/Agent/QwenWorkCN.app/Contents/Resources/icon.icns"
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$APP/Contents/Resources/icon.icns"
fi

# ── 刷新 LaunchServices，让名称/图标立刻生效 ─────────────────────────
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

echo "✓ 已生成：$APP"
echo "  双击打开，或拖到程序坞。日志：~/.qwen-work-switcher/gui-launch.log"
echo "  注意：改过 gui.py / switcher.py 后要重跑本脚本，才会同步进 .app。"
