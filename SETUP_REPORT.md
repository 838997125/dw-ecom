# 医药电商数据仓库 - 环境安装报告

**安装时间**: 2026-07-22 09:21~09:39 (GMT+8)
**机器**: Mac mini (Apple Silicon, macOS Darwin 25.2.0 arm64)

## ✅ 已安装组件（全部完成）

### 1. Python 3.12.8
- **路径**: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`
- **安装方式**: python.org 官方 macOS universal installer
- **已加入 PATH**: `~/.zshrc`
- **依赖库**: oracledb 4.0.2, duckdb 1.5.4, pandas 3.0.3, pyarrow 25.0.0

### 2. DuckDB CLI v1.5.4
- **路径**: `~/.local/bin/duckdb`
- **安装方式**: GitHub Release 二进制 (universal binary)

### 3. DuckDB Python v1.5.4
- **安装方式**: `python3.12 -m pip install duckdb`

### 4. DWS CLI v1.0.54 (钉钉工作台命令行工具)
- **路径**: `~/.local/bin/dws`
- **安装方式**: `npm install -g --prefix ~/.local dingtalk-workspace-cli`
- **Skills**: 已安装到 `~/.openclaw/skills/dws` 和 `~/.agents/skills/dws` (mono 模式)
- **状态**: ⚠️ 未登录（需执行 `dws auth login` 扫码授权）

### 5. oracledb v4.0.2 (Python)
- **模式**: Thin mode（无需 Oracle Instant Client）
- **目标**: 连接 Oracle 12c (<ORACLE_HOST>)

### 6. pandas v3.0.3 + pyarrow v25.0.0
- **用途**: 数据处理 + Parquet 归档

## ⚠️ 待处理

### Homebrew
- **状态**: 未安装（GitHub 连接不稳定，git clone 超时）
- **影响**: 不阻塞项目 -- 所有核心组件已通过其他方式安装
- **后续**: 网络恢复后可手动 `git clone https://github.com/Homebrew/brew /opt/homebrew` 安装

## 📁 项目结构

```
~/.openclaw/workspace/dw-ecom/
├── config/
│   └── config.example.yaml      # 配置模板
├── data/
│   ├── duckdb/
│   │   └── ecom.duckdb          # ✅ 已初始化的数仓数据库
│   └── parquet/                 # Parquet 归档目录
├── sql/
│   ├── init_schema.sql          # ✅ 三层结构初始化 SQL
│   ├── raw/                     # raw 层 SQL
│   ├── agg/                     # agg 层 SQL
│   └── dim/                     # dim 层 SQL
├── src/
│   ├── cdc/                     # CDC 抽取脚本
│   ├── agg/                     # 聚合脚本
│   ├── dim/                     # 维度同步脚本
│   ├── push/                    # 钉钉推送脚本
│   ├── alert/                   # 告警脚本
│   └── utils/                   # 工具函数
├── logs/
├── archive/
└── scripts/                     # crontab 脚本
```

## 🗄️ DuckDB 数仓结构（已初始化）

| Schema | 表名 | 用途 |
|--------|------|------|
| **dim** | customer | 客户维度表 |
| **dim** | product | 商品维度表 |
| **dim** | warehouse | 仓库维度表 |
| **raw** | orders | 订单原始数据 |
| **raw** | order_lines | 订单明细原始数据 |
| **raw** | inventory | 库存原始数据 |
| **raw** | _cdc_state | CDC 增量同步状态 |
| **agg** | kpi_realtime | KPI 实时仪表盘 |
| **agg** | daily_sales | 日报销售汇总 |
| **agg** | alert_inventory | 库存异常告警 |
| **agg** | weekly_report | 周报 |
| **agg** | monthly_report | 月报 |
| **agg** | data_audit | 数据对账记录 |

## 📋 下一步操作

### 需要你手动完成：
1. **DWS CLI 授权**: 在终端执行 `~/.local/bin/dws auth login`，扫码登录钉钉
2. **Oracle 连接信息**: 确认 service_name 和 READONLY_DB_USER 密码，填入 `config/config.yaml`
3. **钉钉群机器人**: 创建群机器人，获取 webhook URL 和 secret

### 我接下来可以做的：
1. 查 Oracle READONLY_DB_USER 用户的表权限和 schema（需要 Oracle 连接信息）
2. 根据实际 Oracle 表结构调整 raw 层 schema
3. 写 CDC 增量抽取脚本
4. 创建钉钉多维表格（DWS CLI 授权后）
5. 配置 crontab 调度
