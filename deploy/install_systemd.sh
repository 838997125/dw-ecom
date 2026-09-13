#!/usr/bin/env bash
# ============================================================
# 注册 dw-ecom 三个 systemd 服务（开机自启 + 崩溃重启）
# 需 root。在项目根目录执行： sudo bash deploy/install_systemd.sh
# 卸载： sudo bash deploy/install_systemd.sh --remove
# ============================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SRC_DIR/.." && pwd)"
SERVICES=(dw-ecom-cdc-daemon dw-ecom-streamlit dw-ecom-mcp-http)
TARGET=/etc/systemd/system

ACTION="${1:-install}"
if [ "$ACTION" = "--remove" ]; then
    for svc in "${SERVICES[@]}"; do
        systemctl disable --now "$svc.service" 2>/dev/null || true
        rm -f "$TARGET/$svc.service"
        echo "已移除 $svc"
    done
    systemctl daemon-reload
    echo "完成。"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行（需要写 $TARGET）" >&2
    exit 1
fi

RUN_USER="${SUDO_USER:-$(whoami)}"
RUN_HOME="$(eval echo "~$RUN_USER")"

echo "安装目录 : $INSTALL_DIR"
echo "运行用户 : $RUN_USER"

for svc in "${SERVICES[@]}"; do
    src="$SRC_DIR/$svc.service"
    dst="$TARGET/$svc.service"
    sed \
        -e "s#__USER__#$RUN_USER#g" \
        -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
        "$src" > "$dst"
    echo "已写入 $dst"
done

# .env 权限：仅运行用户可读（含密码）
chown "$RUN_USER": "$INSTALL_DIR/.env" 2>/dev/null || true
chmod 600 "$INSTALL_DIR/.env" 2>/dev/null || true

systemctl daemon-reload
for svc in "${SERVICES[@]}"; do
    systemctl enable "$svc.service"
done

cat <<EOF

服务已注册（尚未启动）。建议按顺序启动并逐个确认：
  sudo systemctl start dw-ecom-cdc-daemon
  systemctl status dw-ecom-cdc-daemon --no-pager
  sudo systemctl start dw-ecom-mcp-http
  sudo systemctl start dw-ecom-streamlit

  仪表盘: http://<服务器IP>:8501/dashboard
  MCP 健康检查: curl http://127.0.0.1:8765/health
  MCP API Key 见 .env 的 MCP_API_KEY（未设置则首次启动自动生成在
  duckdb-mcp-server/.api_key）
EOF
