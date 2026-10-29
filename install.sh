#!/bin/bash
# 安装千问办公账户切换器到 ~/.local/bin/qwenwork-switch
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
TARGET="$BIN_DIR/qwenwork-switch"

mkdir -p "$BIN_DIR"

cat > "$TARGET" <<EOF
#!/bin/bash
exec /usr/bin/env python3 "$SRC_DIR/switcher.py" "\$@"
EOF
chmod +x "$TARGET"

echo "✓ 已安装：$TARGET"
echo "  指向：$SRC_DIR/switcher.py"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "⚠  $BIN_DIR 不在 PATH 中，把下面这行加到 ~/.zshrc 后重开终端："
    echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo ""
echo "试用：qwenwork-switch doctor"
