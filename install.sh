#!/usr/bin/env bash
# ============================================================
# dw-ecom 一键安装脚本（Linux / macOS 通用）
# 作用：建虚拟环境 -> 装依赖 -> 生成配置 -> 初始化空库结构
# 不会启动任何服务，也不会改动系统；systemd 注册是可选步骤。
# 用法：
#   bash install.sh                  # 标准安装
#   bash install.sh --no-init-db     # 只装环境，暂不建库
# ============================================================
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$INSTALL_DIR/.venv"

echo "============================================================"
echo " dw-ecom 安装"
echo " 目录: $INSTALL_DIR"
echo "============================================================"

# 1. Python 版本检查（要求 >= 3.10，3.12 已验证）
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python >= 3.10（生产环境为 3.12）。当前: %s" % sys.version.split()[0])
print("Python 版本 OK:", sys.version.split()[0])
PY

# 2. 虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/5] 创建虚拟环境 .venv ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    echo "[1/5] 虚拟环境已存在，跳过创建"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel >/dev/null

# 3. 安装依赖
echo "[2/5] 安装 Python 依赖（可能需要几分钟）..."
python -m pip install -r requirements.txt

# 4. 配置文件
echo "[3/5] 检查配置文件 ..."
if [ ! -f "$INSTALL_DIR/config/config.yaml" ]; then
    cp "$INSTALL_DIR/config/config.example.yaml" "$INSTALL_DIR/config/config.yaml"
    echo "  已从模板生成 config/config.yaml"
    echo "  ⚠️  请填写 Oracle 密码（或配置 .env 里的 ORACLE_PASSWORD）"
else
    echo "  config/config.yaml 已存在，保留不动"
fi
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo "  已生成 .env（请填入密码/密钥）"
else
    echo "  .env 已存在，保留不动"
fi

mkdir -p "$INSTALL_DIR/data/duckdb" "$INSTALL_DIR/logs/cron"

# 5. 初始化库结构（可选）
if [ "${1:-}" != "--no-init-db" ]; then
    echo "[4/5] 初始化 DuckDB 库结构 ..."
    python scripts/init_db.py || {
        echo "  （库已存在会被跳过，属正常；强制重建用 python scripts/init_db.py --force）"
    }
else
    echo "[4/5] 跳过数据库初始化"
fi

echo "[5/5] 安装完成。"
cat <<EOF

============================================================
 下一步
------------------------------------------------------------
 1) 编辑配置：
      vi config/config.yaml      # Oracle 连接信息
      vi .env                    # ORACLE_PASSWORD / MYSQL_PASSWORD / MCP_API_KEY

 2) 手动启动（前台调试）：
      source .venv/bin/activate
      python -m src.cdc.cdc_daemon                      # CDC 同步守护进程
      streamlit run app.py --server.port 8501           # 仪表盘
      cd duckdb-mcp-server && python src/server_http.py # MCP HTTP (8765)

 3) 首批数据（冷启动从 Oracle 全量拉取）：
      python -m src.cdc.cdc full --all

 4) Linux 注册开机自启（可选）：
      sudo bash deploy/install_systemd.sh

 5) macOS launchd（可选）：见 deploy/launchd/README.md
============================================================
EOF
