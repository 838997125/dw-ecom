#!/usr/bin/env bash
# ============================================================
# 注册 dw-ecom 三个 launchd 服务（macOS 开机自启 + 崩溃重启）
# 在项目根目录执行： bash deploy/install_launchd.sh
# 卸载： bash deploy/install_launchd.sh --remove
# ============================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SRC_DIR/.." && pwd)"
LAUNCHD_SRC="$SRC_DIR/launchd"
TARGET="$HOME/Library/LaunchAgents"
PLISTS=(com.dw-ecom.cdc-daemon com.dw-ecom.streamlit com.dw-ecom.mcp-http)

ACTION="${1:-install}"
if [ "$ACTION" = "--remove" ]; then
    for name in "${PLISTS[@]}"; do
        launchctl unload "$TARGET/$name.plist" 2>/dev/null || true
        rm -f "$TARGET/$name.plist"
        echo "已移除 $name"
    done
    exit 0
fi

mkdir -p "$TARGET"
for name in "${PLISTS[@]}"; do
    dst="$TARGET/$name.plist"
    sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__HOME__#$HOME#g" \
        "$LAUNCHD_SRC/$name.plist" > "$dst"
    echo "已写入 $dst"
done

echo
echo "加载并启动（逐个）："
for name in "${PLISTS[@]}"; do
    launchctl unload "$TARGET/$name.plist" 2>/dev/null || true
    launchctl load "$TARGET/$name.plist"
    echo "  loaded $name"
done

cat <<EOF

完成。常用排查命令：
  launchctl list | grep dw-ecom
  tail -f "$INSTALL_DIR/logs/cron/daemon_stdout.log"
  tail -f "$INSTALL_DIR/logs/streamlit.log"
EOF
