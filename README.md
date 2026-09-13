# dw-ecom — 医药电商数据仓库一体机

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Streamlit 看板 + Oracle CDC 实时同步 + DuckDB 列存数仓 + DuckDB MCP Server，
一套从 **时空 ERP(Oracle)** 抽数、在本地 **DuckDB** 建仓、通过
**Streamlit** 可视化、并以 **MCP(HTTP)** 对外提供只读查询的完整系统。

> 本仓库为**可重新部署的代码包**：不含业务数据（3GB+ DuckDB 文件）、
> 不含日志、不含任何密码/密钥。在新服务器上 `clone → 配置 → install → 同步` 即可运行。

---

## 一、系统架构

```
                 ┌─────────────────────────── 新服务器 (Linux / macOS) ───────────────────────────┐
                 │                                                                                │
 时空 ERP        │   ┌──────────────┐    增量/全量     ┌──────────────────────────────────────┐    │
 Oracle RACE ────┼──▶│ cdc_daemon   │ ───────────────▶ │  DuckDB  data/duckdb/ecom.duckdb     │    │
 (内网地址)      │   │ (常驻进程)    │                  │  raw(44) / dim(5) / agg(7) / ymss(16)│    │
                 │   └──────────────┘                  └───────────────┬──────────────────────┘    │
 (可选) MySQL    │   ┌──────────────┐  每日镜像                      │ 只读连接                   │
 YMSS_DING/… ────┼──▶│ sync_mysql…  │ ───────────────┐               │                            │
                 │   └──────────────┘                ▼               ▼                            │
                 │                         ┌──────────────────┐  ┌───────────────────────┐        │
                 │                         │ Streamlit :8501   │  │ MCP HTTP Server :8765 │        │
                 │                         │ /dashboard 看板    │  │ /mcp (X-API-Key 鉴权)  │        │
                 │                         └──────────────────┘  └───────────────────────┘        │
                 └────────────────────────────────────────────────────────────────────────────────┘
```

| 组件 | 入口 | 端口 | 说明 |
|---|---|---|---|
| CDC 守护进程 | `python -m src.cdc.cdc_daemon` | — | 单进程串行调度，替代 31 条 crontab，持写锁零冲突 |
| Streamlit 看板 | `streamlit run app.py` | 8501 | 销售/库存/退货监控等，路径前缀 `/dashboard` |
| MCP HTTP | `duckdb-mcp-server/src/server_http.py` | 8765 | `query_duckdb` / `get_schema` / `get_kpi`，强制只读 |

**数据源**：时空 ERP Oracle（schema `RACE`）；可选本地 MySQL（`YMSS_DING/DWD/DWS`）。

---

## 二、目录结构

```
dw-ecom/
├── app.py                     # Streamlit 主应用
├── envloader.py               # 零依赖 .env 加载器（各入口自动调用）
├── requirements.txt
├── install.sh                 # ★ 一键安装（venv + 依赖 + 配置 + 建库）
├── .env.example               # 密码/密钥环境变量模板
├── config/
│   ├── config.example.yaml    # 配置模板（Oracle 地址、CDC 表与水位线规则）
│   └── config.yaml            # 实际配置（.gitignore，安装时从模板生成）
├── src/
│   ├── cdc/                   # CDC 引擎 + 常驻调度 daemon
│   ├── agg/                   # C 端零售聚合刷新
│   └── utils/                 # db.py(Oracle/DuckDB连接+写锁+密钥解析)、health 等
├── duckdb-mcp-server/
│   └── src/
│       ├── server.py          # MCP 核心查询逻辑（stdio 可复用）
│       └── server_http.py     # FastAPI HTTP 封装（/mcp、/health）
├── sql/
│   ├── schema_full.sql        # ★ 72 张表完整结构（建库种子，由生产库导出）
│   ├── init_schema.sql / dim_schema.sql / raw_schema*.sql
├── scripts/
│   ├── init_db.py             # ★ 冷启动建库（幂等，含 --force）
│   ├── full_sync_batch.sh     # 全量批量同步
│   ├── sync_mysql_to_duckdb.py
│   └── ...                    # 校验、压缩、近效期/缺货报表等运维脚本
└── deploy/
    ├── *.service              # systemd 单元（Linux）
    ├── install_systemd.sh     # ★ Linux 注册开机自启
    └── launchd/ + install_launchd.sh   # macOS 开机自启
```

---

## 三、新服务器快速安装

### 前置要求
- Python **3.10+**（生产在 3.12 验证；建议 3.12）
- 能访问时空 ERP 的 Oracle（默认 `<ORACLE_HOST>:1521/<SERVICE_NAME>`）
  - `oracledb` 用 **thin 模式**，Linux 上通常无需安装 Oracle Instant Client
- （可选）本地 MySQL，仅当需要 `YMSS_*` 镜像时

### 步骤

```bash
# 1) 拉代码
git clone https://github.com/838997125/dw-ecom.git
cd dw-ecom

# 2) 一键安装（建 .venv、装依赖、生成配置、初始化 72 张空表）
bash install.sh

# 3) 填配置（二选一，或都填；环境变量优先级更高）
vi config/config.yaml      # Oracle host/user/service_name
vi .env                    # ORACLE_PASSWORD=... 、MYSQL_PASSWORD、MCP_API_KEY

# 4) 首批数据：从 Oracle 全量拉取（耗时取决于数据量）
source .venv/bin/activate
python -m src.cdc.cdc full --all

# 5) 启动常驻 CDC（之后按内置调度自动增量）
python -m src.cdc.cdc_daemon
```

### 启动看板与 MCP（可另开终端前台调试）

```bash
source .venv/bin/activate
streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
# 看板： http://<服务器IP>:8501/dashboard

cd duckdb-mcp-server
MCP_HOST=0.0.0.0 MCP_PORT=8765 python src/server_http.py
# 健康检查： curl http://127.0.0.1:8765/health
```

### 注册开机自启

- **Linux（systemd，推荐生产）**：
  ```bash
  sudo bash deploy/install_systemd.sh
  sudo systemctl start dw-ecom-cdc-daemon
  sudo systemctl start dw-ecom-mcp-http dw-ecom-streamlit
  systemctl status dw-ecom-cdc-daemon --no-pager
  ```
  卸载：`sudo bash deploy/install_systemd.sh --remove`

- **macOS（launchd）**：
  ```bash
  bash deploy/install_launchd.sh
  launchctl list | grep dw-ecom
  ```
  卸载：`bash deploy/install_launchd.sh --remove`

---

## 四、配置与密钥说明

- `config/config.yaml`：Oracle 连接、DuckDB 路径、**每张同步表的 CDC 水位线字段/主键/明细联动**。
  该文件已被 `.gitignore` 忽略，安装时从 `config.example.yaml` 复制。
- `.env`：放密码与密钥，**不入库**，权限建议 `600`：
  | 变量 | 用途 |
  |---|---|
  | `ORACLE_PASSWORD` | 时空 ERP 密码（最高优先级） |
  | `MYSQL_HOST/PORT/USER/PASSWORD` | 可选 MySQL 镜像 |
  | `DUCKDB_PATH` | 覆盖默认库路径 |
  | `ECOM_WRITE_LOCK` | 覆盖写锁文件路径（默认 `/tmp/ecom_duckdb_write.lock`） |
  | `MCP_API_KEY` | MCP 鉴权 Key；不填则首启自动生成到 `duckdb-mcp-server/.api_key` |
  | `MCP_HOST/MCP_PORT` | MCP 监听（默认 `127.0.0.1:8765`） |

密码解析规则（`src/utils/db.py::resolve_secret`）：
`${ENV_VAR}` 占位符 → 指定环境变量 → config 明文。systemd 通过 `EnvironmentFile=.env` 注入，
手动运行时由 `envloader.py` 自动加载。

> 若把 MCP 直接暴露公网，请前置反代/HTTPS，并务必设置强随机 `MCP_API_KEY`；
> 生产原环境用 Cloudflare/ngrok 隧道（脚本见运维文档），本仓库不含隧道。

---

## 五、运维常用命令

```bash
source .venv/bin/activate

# CDC
python -m src.cdc.cdc status            # 各表同步水位线/状态
python -m src.cdc.cdc incremental --table SALEOUTMT
python -m src.cdc.cdc full --table GOODSDOC
python -m src.cdc.cdc verify --all      # Oracle/DuckDB 行数对账
python -m src.utils.health              # 健康检查

# 库维护
python scripts/init_db.py               # 幂等建库（已存在会拒绝，--force 清空重建）
python scripts/compact_db.py            # 压缩
python scripts/safe_vacuum.py           # 安全 VACUUM（daemon 也会定时执行）

# 日志
tail -f logs/cron/daemon.log
tail -f logs/cdc.log
```

内置 daemon 调度节奏（节选）：销售/出库/退款 5 分钟；库存/采购 15 分钟；
主数据 6 小时；WMS/票据快照每日凌晨全量；每日 02:00 对账、04:00 VACUUM；
周日 compact；每日 07:40 MySQL 镜像。完整表见 `src/cdc/cdc_daemon.py` 的 `SCHEDULE`。

---

## 六、从生产环境迁移已有数据（可选，跳过冷同步）

仓库默认**不带数据**，新服务器靠 CDC 全量同步即可。
若希望直接拷贝现有数仓，在**停机窗口**复制生产机的库文件（注意先停 CDC 写进程）：

```bash
# 生产机（Mac mini）
#   ~/.openclaw/workspace/dw-ecom/data/duckdb/ecom.duckdb
scp /path/ecom.duckdb user@新服务器:/opt/dw-ecom/data/duckdb/ecom.duckdb
```
DuckDB 单文件、跨平台（macOS/Linux）兼容。

---

## 七、注意事项 / 已知边界

- **DuckDB 单写多读**：所有写进程通过 `fcntl.flock` 串行化（默认锁 `/tmp/ecom_duckdb_write.lock`）；
  MCP/看板均用短连接只读访问，写时只读会短暂重试。
- Streamlit「退货同步监控」页读取兄弟项目 `../refund-sync` 的状态文件；
  新服务器未部署该项目时该页会提示“未找到状态文件”，**不影响其他功能**。
- `scripts/check_angleid.py` 在历史源码中存在语法残缺，本仓库已修正。
- 隧道（ngrok/cloudflared）、钉钉 dws CLI 等外部集成不在本仓库范围。

---

## 八、版本基线

- Python 3.12（>=3.10 可用）；依赖版本见 `requirements.txt`（对齐 2026-09 生产环境）
- 表结构快照：`sql/schema_full.sql`，72 张表（raw 44 / dim 5 / agg 7 / ymss 16）

---

## 九、开源协议

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改、分发与商用，
唯一要求是保留版权与许可声明。
