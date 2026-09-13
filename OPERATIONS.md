# 医药电商数据仓库 - 运维使用指南

## 📁 项目目录结构

```
~/.openclaw/workspace/dw-ecom/
├── config/
│   ├── config.yaml              # 实际配置（含密码）
│   └── config.example.yaml      # 配置模板
├── data/
│   ├── duckdb/
│   │   └── ecom.duckdb          # DuckDB 数据库文件（本地数仓）
│   └── parquet/                 # Parquet 归档目录
├── sql/
│   ├── init_schema.sql          # 三层结构初始化（agg + dim）
│   ├── raw_schema.sql           # raw 层建表 SQL（基于 Oracle 实际结构）
│   ├── raw/                     # raw 层 SQL
│   ├── agg/                     # agg 层 SQL
│   └── dim/                     # dim 层 SQL
├── src/
│   ├── cdc/
│   │   └── cdc.py               # CDC 同步引擎
│   └── utils/
│       ├── db.py                # 数据库连接管理
│       └── query.py             # 查询工具
├── logs/
│   └── cdc.log                  # CDC 运行日志
├── scripts/                     # crontab 脚本
├── ORACLE_SCHEMA.md             # Oracle 探查报告
└── SETUP_REPORT.md              # 安装报告
```

---

## 🔧 核心命令

### 1. CDC 同步

```bash
# 进入项目目录
cd ~/.openclaw/workspace/dw-ecom

# 确保 Python 3.12 在 PATH 中
export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$HOME/.local/bin:$PATH"

# ---- 全量同步 ----
python3 -m src.cdc.cdc full --table STOREHOUSE       # 同步单表
python3 -m src.cdc.cdc full --table GOODSDOC          # 同步商品
python3 -m src.cdc.cdc full --table SALEOUTMT         # 同步销售出库主表
python3 -m src.cdc.cdc full --all                     # 同步所有配置的表

# ---- 增量同步 ----
python3 -m src.cdc.cdc incremental --table SALEOUTMT  # 增量同步单表
python3 -m src.cdc.cdc incremental --all              # 增量同步所有表

# ---- 查看同步状态 ----
python3 -m src.cdc.cdc status
```

### 2. 查询工具

```bash
# 列出所有表及行数
python3 -m src.utils.query tables

# 今日 KPI 汇总
python3 -m src.utils.query kpi

# 指定日期销售汇总
python3 -m src.utils.query daily --date 2026-07-22

# 自定义 SQL
python3 -m src.utils.query sql "SELECT WHCODE, WHNAME FROM raw.STOREHOUSE WHERE BEACTIVE='Y'"
```

### 3. DuckDB CLI 直接操作

```bash
# 交互式查询
~/.local/bin/duckdb ~/.openclaw/workspace/dw-ecom/data/duckdb/ecom.duckdb

# 执行 SQL 文件
~/.local/bin/duckdb data/duckdb/ecom.duckdb < sql/raw_schema.sql

# 单条查询
~/.local/bin/duckdb data/duckdb/ecom.duckdb -c "SELECT COUNT(*) FROM raw.GOODSDOC"
```

---

## 📊 当前已同步数据

| 表名 | 行数 | 同步时间 |
|------|------|----------|
| STOREHOUSE | 9 | 2026-07-22 10:10 |
| STAFFDOC | 60 | 2026-07-22 10:10 |
| SUPPLYDOC | 876 | 2026-07-22 10:10 |
| GOODSDOC | 47,927 | 2026-07-22 10:10 |
| CLIENTDOC | 15,016 | 2026-07-22 10:11 |

**待同步的大表**（按优先级排序）：
1. SALEOUTMT (65.5万) + SALEOUTDT (84.3万) - 销售出库
2. SALENOTESMT (66.5万) + SALENOTESDT (86.5万) - 销售单
3. BATCHCODE (61.5万) - 批次批号
4. K_D3OMS_STOCKOUTMT_PLAN (62.4万) - OMS出库计划
5. PURORDERMT + PURORDERDT - 采购订单
6. PURINMT + PURINDT - 采购入库
7. ANGLEBALANCE - 库位库存
8. K_D3OMS_ORDERREFUNDMT + DT - 退款

---

## 🔄 CDC 增量同步原理

### 工作流程

```
Oracle (RACE Schema)
    │
    │ 1. 读取 raw._cdc_state 获取上次同步时间点
    │ 2. 查询 Oracle: WHERE LASTMODIFYTIME > 上次时间点
    │ 3. 对每行先 DELETE (按主键) 再 INSERT (upsert)
    │ 4. 如有明细表，用主表新 BILLNO 关联查询明细
    │ 5. 更新 raw._cdc_state 记录新的时间点
    │
    ▼
DuckDB raw 层
```

### 增量同步配置（config.yaml）

每张表配置了：
- `cdc_column`: 增量字段（LASTMODIFYTIME 或 SYSDATES）
- `primary_key`: 主键（用于 upsert）
- `detail_table`: 关联明细表（可选）
- `detail_fk`: 明细表外键
- `detail_pk`: 明细表主键

### 明细表同步策略

明细表没有独立的时间字段，通过主表的新增 BILLNO 反查：
1. 找出主表本次增量新增/修改的 BILLNO
2. 用这些 BILLNO 去 Oracle 查对应的明细行
3. 先删后插到 DuckDB

---

## ⏰ crontab 调度配置

在 Mac mini 上配置 crontab 实现自动化：

```bash
# 编辑 crontab
crontab -e
```

建议配置：

```crontab
# ============================================
# 医药电商数据仓库 - crontab 调度
# ============================================
# 环境变量
PATH=/Library/Frameworks/Python.framework/Versions/3.12/bin:/Users/<USER>/.local/bin:/usr/bin:/bin
WORKDIR=/Users/<USER>/.openclaw/workspace/dw-ecom

# ---- 分钟级（每5分钟）----
# 销售出库主表增量同步
*/5 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table SALEOUTMT >> logs/cron_saleoutmt.log 2>&1

# ---- 15分钟级 ----
# OMS出库计划 + 退款
*/15 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table K_D3OMS_STOCKOUTMT_PLAN >> logs/cron_oms.log 2>&1

# ---- 小时级（每小时第10分钟）----
# 采购订单 + 入库
10 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table PURORDERMT >> logs/cron_pur.log 2>&1
10 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table PURINMT >> logs/cron_pur.log 2>&1

# ---- 6小时级（每天 0/6/12/18 点第20分钟）----
# 商品/客户主数据
20 0,6,12,18 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table GOODSDOC >> logs/cron_dim.log 2>&1
20 0,6,12,18 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table CLIENTDOC >> logs/cron_dim.log 2>&1

# ---- 日级（每天凌晨2点）----
# 批次/库存/供应商/仓库/员工
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table BATCHCODE >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table ANGLEBALANCE >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table SUPPLYDOC >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table STOREHOUSE >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table STAFFDOC >> logs/cron_daily.log 2>&1

# ---- 状态报告（每天早上8点）----
0 8 * * * cd $WORKDIR && python3 -m src.cdc.cdc status >> logs/cron_status.log 2>&1
```

---

## 📋 日常运维

### 检查同步状态
```bash
python3 -m src.cdc.cdc status
```

### 查看日志
```bash
# CDC 日志
tail -100 ~/.openclaw/workspace/dw-ecom/logs/cdc.log

# crontab 日志
tail -100 ~/.openclaw/workspace/dw-ecom/logs/cron_saleoutmt.log
```

### 重建某张表（全量重灌）
```bash
python3 -m src.cdc.cdc full --table SALEOUTMT
```

### DuckDB 数据库维护
```bash
# 压缩数据库（清理碎片）
~/.local/bin/duckdb data/duckdb/ecom.duckdb -c "VACUUM;"

# 检查数据库大小
ls -lh data/duckdb/ecom.duckdb
```

### 首次全量同步（新环境初始化）
```bash
# 1. 初始化 schema
~/.local/bin/duckdb data/duckdb/ecom.duckdb < sql/init_schema.sql
~/.local/bin/duckdb data/duckdb/ecom.duckdb < sql/raw_schema.sql

# 2. 全量同步所有表（按优先级）
python3 -m src.cdc.cdc full --table STOREHOUSE
python3 -m src.cdc.cdc full --table STAFFDOC
python3 -m src.cdc.cdc full --table SUPPLYDOC
python3 -m src.cdc.cdc full --table GOODSDOC
python3 -m src.cdc.cdc full --table CLIENTDOC
python3 -m src.cdc.cdc full --table SALEOUTMT     # ~2分钟
python3 -m src.cdc.cdc full --table SALENOTESMT
python3 -m src.cdc.cdc full --table BATCHCODE
# ... 其他表

# 3. 验证
python3 -m src.cdc.cdc status
python3 -m src.utils.query tables
```

---

## ⚠️ 已知问题

1. **GOODSDOC 有重复主键**: Oracle 侧 GOODSID 存在重复值，全量同步时会跳过重复行（不影响使用）
2. **时间字段是 CHAR 类型**: Oracle 的 DATES/ONTIME/LASTMODIFYTIME 都是 CHAR，不是 TIMESTAMP，查询时需要 `TRY_CAST(DATES AS DATE)` 转换
3. **明细表无独立时间**: 通过主表 BILLNO 反查明细，增量同步时明细表数据会有少量延迟
4. **executemany 兼容性**: 使用逐行插入替代 executemany，大表同步稍慢但更稳定
