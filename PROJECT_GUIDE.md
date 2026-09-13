# 医药电商数据仓库 - 项目体系与使用指南

## 一、整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Oracle 12c (时空 ERP)                         │
│                   <ORACLE_HOST>:1521/<SERVICE_NAME>                   │
│                   RACE Schema · 27 张表 · READONLY_DB_USER 只读               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    Python oracledb
                    (thin mode, 增量 CDC)
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    Mac mini (M4 Pro / 48GB)                      │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              DuckDB 本地数仓 (ecom.duckdb)               │    │
│  │                                                         │    │
│  │  raw 层 (19张表)    ──→  agg 层 (6张表)  ──→  dim 层    │    │
│  │  Oracle 镜像数据         聚合/KPI/告警      维度表       │    │
│  │  65万行销售/84万明细     日报/周报/月报     客户/商品    │    │
│  └─────────────────────────────────────────────────────────┘    │
│                           │                                      │
│              ┌────────────┼────────────┐                         │
│              ▼            ▼            ▼                         │
│  ┌──────────────┐ ┌──────────┐ ┌───────────────┐                │
│  │ DWS CLI      │ │ crontab  │ │ OpenClaw      │                │
│  │ → 钉钉多维表格│ │ 五级调度  │ │ AI 查询/告警  │                │
│  └──────────────┘ └──────────┘ └───────────────┘                │
│         │                                          │             │
└─────────┼──────────────────────────────────────────┼─────────────┘
          │                                          │
          ▼                                          ▼
  ┌────────────────┐                    ┌──────────────────────┐
  │  钉钉消费层     │                    │  企业微信/OpenClaw   │
  │                │                    │                      │
  │ · KPI 仪表盘   │                    │ · 自然语言查数       │
  │ · 日报/周报     │                    │ · 智能告警推送       │
  │ · 库存告警     │                    │ · 异常处理决策       │
  │ · 主数据看板   │                    │ · 邮件/消息通知      │
  └────────────────┘                    └──────────────────────┘
```

---

## 二、DuckDB CDC 详解

### 什么是 CDC？

CDC = Change Data Capture（变更数据捕获）。本项目的 CDC 采用**时间戳轮询**方式：

1. DuckDB 里维护一张 `_cdc_state` 表，记录每张表最后同步到的时间点
2. 每次同步时，查询 Oracle 中 `LASTMODIFYTIME > 上次时间点` 的数据
3. 对这些数据做 upsert（先按主键删除旧行，再插入新行）
4. 更新 `_cdc_state` 的时间点到 Oracle 当前最新值

### 两种同步模式

#### 全量同步 (full)

用途：首次初始化、重建某张表

```bash
cd ~/.openclaw/workspace/dw-ecom
export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$HOME/.local/bin:$PATH"

# 同步单表
python3 -m src.cdc.cdc full --table SALEOUTMT

# 同步所有配置的表
python3 -m src.cdc.cdc full --all
```

流程：
1. 查询 Oracle 获取该表所有列名
2. 与 DuckDB 表列取交集（只同步匹配的列）
3. `DELETE FROM raw.表名` 清空 DuckDB 表
4. 分批从 Oracle 读取（每批 5000 行），逐行插入 DuckDB
5. 记录同步状态到 `_cdc_state`

#### 增量同步 (incremental)

用途：日常运行，只同步变化的数据

```bash
# 增量同步单表
python3 -m src.cdc.cdc incremental --table SALEOUTMT

# 增量同步所有表
python3 -m src.cdc.cdc incremental --all
```

流程：
1. 从 `_cdc_state` 读取上次同步时间点
2. 查询 Oracle `WHERE LASTMODIFYTIME > 上次时间点`
3. 对每行先 DELETE（按主键）再 INSERT
4. 如果配置了明细表，用主表新 BILLNO 反查明细数据
5. 更新 `_cdc_state` 时间点

### 查看同步状态

```bash
python3 -m src.cdc.cdc status
```

输出示例：
```
表名               最后CDC时间              记录数  同步时间              状态     模式
------------------------------------------------------------------------------------------
SALEOUTMT         2026-07-22 10:30:00     1250  2026-07-22 10:35:00  SUCCESS  INCREMENTAL
GOODSDOC          2026-07-22 09:58:22    51429  2026-07-22 10:10:57  SUCCESS  FULL
```

### 配置说明 (config.yaml)

```yaml
cdc:
  incremental:
    - table: SALEOUTMT              # Oracle 表名
      cdc_column: "LASTMODIFYTIME"  # 增量字段
      cdc_format: "YYYY-MM-DD HH24:MI:SS"  # Oracle 时间格式
      primary_key: "BILLNO"         # 主键（用于 upsert）
      detail_table: SALEOUTDT       # 关联明细表（可选）
      detail_fk: "BILLNO"           # 明细表外键
      detail_pk: "BILLNO,BILLSN"    # 明细表主键
```

---

## 三、查询工具详解

### 命令一览

```bash
cd ~/.openclaw/workspace/dw-ecom
export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$HOME/.local/bin:$PATH"

# 1. 列出所有表及行数
python3 -m src.utils.query tables

# 2. 今日 KPI 汇总（销售额、毛利、退款、商品数等）
python3 -m src.utils.query kpi

# 3. 指定日期销售汇总（按仓库、按客户类型）
python3 -m src.utils.query daily --date 2026-07-22

# 4. 自定义 SQL
python3 -m src.utils.query sql "你的 SQL 语句"
```

### DuckDB CLI 交互模式

```bash
# 打开交互式 DuckDB
~/.local/bin/duckdb ~/.openclaw/workspace/dw-ecom/data/duckdb/ecom.duckdb

# 常用查询示例：

-- 今日销售出库汇总
SELECT COUNT(*) as 单数, SUM(TAXAMOUNT) as 含税金额, SUM(PROFIT) as 毛利
FROM raw.SALEOUTMT
WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE;

-- 按商品查销售 TOP 20
SELECT d.GOODSNAME, SUM(d.NUM) as 数量, SUM(d.AMOUNT) as 金额
FROM raw.SALEOUTDT d
JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
WHERE TRY_CAST(m.DATES AS DATE) >= CURRENT_DATE - 7
GROUP BY d.GOODSNAME
ORDER BY 金额 DESC
LIMIT 20;

-- 库存预警（低于最小库存）
SELECT g.GOODSNAME, a.PLACENUM as 当前库存, a.STORMIN as 最小库存
FROM raw.ANGLEBALANCE a
JOIN raw.GOODSDOC g ON a.GOODSID = g.GOODSID
WHERE a.PLACENUM < a.STORMIN AND a.STORMIN > 0;

-- 退出
.quit
```

---

## 四、crontab 自动调度

### 配置方法

```bash
crontab -e
```

### 调度方案

```crontab
PATH=/Library/Frameworks/Python.framework/Versions/3.12/bin:/Users/<USER>/.local/bin:/usr/bin:/bin
WORKDIR=/Users/<USER>/.openclaw/workspace/dw-ecom

# === 分钟级（每5分钟）===
# 销售出库增量同步（KPI 核心）
*/5 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table SALEOUTMT >> logs/cron_saleoutmt.log 2>&1

# === 15分钟级 ===
# OMS 出库计划 + 退款
*/15 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table K_D3OMS_STOCKOUTMT_PLAN >> logs/cron_oms.log 2>&1
*/15 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table K_D3OMS_ORDERREFUNDMT >> logs/cron_oms.log 2>&1

# === 小时级（每小时第10分钟）===
# 采购订单 + 入库
10 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table PURORDERMT >> logs/cron_pur.log 2>&1
10 * * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table PURINMT >> logs/cron_pur.log 2>&1

# === 6小时级（0/6/12/18点第20分钟）===
# 商品/客户主数据
20 0,6,12,18 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table GOODSDOC >> logs/cron_dim.log 2>&1
20 0,6,12,18 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table CLIENTDOC >> logs/cron_dim.log 2>&1

# === 日级（凌晨2点）===
# 批次/库存/供应商/仓库/员工
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table BATCHCODE >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table ANGLEBALANCE >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table SUPPLYDOC >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table STOREHOUSE >> logs/cron_daily.log 2>&1
0 2 * * * cd $WORKDIR && python3 -m src.cdc.cdc incremental --table STAFFDOC >> logs/cron_daily.log 2>&1

# === 状态报告（每天8点）===
0 8 * * * cd $WORKDIR && python3 -m src.cdc.cdc status >> logs/cron_status.log 2>&1
```

### 日志查看

```bash
# 实时查看 CDC 日志
tail -f ~/.openclaw/workspace/dw-ecom/logs/cdc.log

# 查看 crontab 运行日志
tail -50 ~/.openclaw/workspace/dw-ecom/logs/cron_saleoutmt.log
```

---

## 五、后续支持的使用场景

### 场景 1：实时 KPI 仪表盘

**频率**：每 5 分钟刷新

**数据流**：Oracle → CDC → DuckDB raw.SALEOUTMT → agg.kpi_realtime → 钉钉多维表格

**展示内容**：
- 今日销售额（含税/不含税）
- 今日毛利、毛利率
- 今日订单数、退单数
- 同比昨日、环比上周
- 目标完成率

**使用方式**：
```bash
# 查看今日 KPI
python3 -m src.utils.query kpi

# 推送到钉钉多维表格（待开发 push 脚本）
python3 -m src.push.kpi_push
```

---

### 场景 2：销售日报/周报/月报

**频率**：日报每天 8:00，周报每周一 8:30，月报每月 1 号 9:00

**数据流**：DuckDB raw 层 → agg.daily_sales → 钉钉多维表格 append

**展示内容**：
- 按日/周/月汇总销售额、毛利、订单数
- 按仓库、按客户类型、按商品分类维度
- 同比、环比、趋势

**使用方式**：
```bash
# 查看指定日期销售汇总
python3 -m src.utils.query daily --date 2026-07-21

# 生成日报（待开发）
python3 -m src.agg.daily_report --date 2026-07-22
```

---

### 场景 3：库存异常告警

**频率**：每 3 分钟检查

**数据流**：DuckDB raw.ANGLEBALANCE → 阈值检测 → agg.alert_inventory → 钉钉群机器人推送

**告警类型**：
- 低库存：PLACENUM < STORMIN
- 积压：PLACENUM > STORMAX
- 近效期：VALDATE - CURRENT_DATE < 90 天
- 断货：PLACENUM = 0 且有销售记录

**使用方式**（待开发）：
```bash
# 手动触发库存检查
python3 -m src.alert.inventory_check

# 告警会自动推送到钉钉群
```

---

### 场景 4：自然语言查数（NL → SQL）

**通过 OpenClaw**，直接用自然语言查询数据。

**示例对话**：
- "今天销售额多少？"
- "本周 TOP 10 畅销商品是什么？"
- "上个月哪个仓库发货最多？"
- "客户 XX 最近有没有下单？"
- "对比上月，哪个品类下滑最厉害？"

**实现方式**：OpenClaw 读取 DuckDB schema，将自然语言翻译成 SQL，执行后返回结果。

---

### 场景 5：数据对账

**频率**：每小时

**内容**：
- Oracle 行数 vs DuckDB 行数对比
- 金额合计对比（防丢失/重复）
- 关键单据状态同步检查

**使用方式**（待开发）：
```bash
python3 -m src.utils.audit
```

---

### 场景 6：电商退款监控

**频率**：每 15 分钟

**数据流**：Oracle K_D3OMS_ORDERREFUNDMT/DT → DuckDB → 钉钉告警

**监控内容**：
- 退款单数、退款金额
- 退款原因分类
- 异常退款（大额、频繁）自动告警

---

### 场景 7：采购到货跟踪

**频率**：每小时

**内容**：
- 采购订单 vs 入库对比（待到货、部分到货、已到货）
- 到货时效分析
- 供应商交货及时率

---

### 场景 8：商品主数据看板

**频率**：6 小时同步

**内容**：
- 商品总数、活跃商品数
- 新增商品（近 7 天）
- 停用/冻结商品
- 缺条码商品
- 医保编码覆盖率

---

### 场景 9：Parquet 归档

**频率**：每天凌晨 2:30

**目的**：把 raw 层历史数据导出为 Parquet 格式，压缩存储，保留 1 年。

```bash
# 导出（待开发）
python3 -m src.utils.archive --table SALEOUTMT --date 2026-07
```

---

### 场景 10：钉钉多维表格消费层

**8 张钉钉多维表格**：

| 表名 | 同步方式 | 频率 | 用途 |
|------|----------|------|------|
| KPI_实时仪表盘 | upsert | 5分钟 | 今日核心 KPI |
| 日报_销售汇总 | append | 每日 | 每日销售汇总 |
| 异常_库存告警 | append | 实时 | 库存异常记录 |
| 主数据_客户 | upsert | 6小时 | 客户档案 |
| 主数据_商品 | upsert | 6小时 | 商品档案 |
| 周报 | append | 每周 | 周度汇总 |
| 月报 | append | 每月 | 月度汇总 |
| 数据对账 | append | 每小时 | 对账记录 |

**使用方式**：
```bash
# 创建多维表格（待开发，需 DWS CLI）
dws aitable create --name "医药电商-KPI仪表盘"

# 推送数据
dws aitable record create --base-id xxx --table-id xxx --data @kpi.json
```

---

### 场景 11：钉钉群机器人告警

**告警通道**：DWS CLI → 钉钉群机器人

**告警类型**：
- 🔴 库存预警（低库存/断货）
- 🟡 大额退款告警
- 🟢 每日 KPI 播报
- 🔵 数据同步异常

---

## 六、文件索引

| 文件 | 说明 |
|------|------|
| `config/config.yaml` | 数据库连接、CDC 配置 |
| `sql/init_schema.sql` | DuckDB 三层结构初始化 |
| `sql/raw_schema.sql` | raw 层建表 SQL（基于 Oracle 实际结构） |
| `src/cdc/cdc.py` | CDC 同步引擎（full/incremental/status） |
| `src/utils/db.py` | Oracle + DuckDB 连接管理 |
| `src/utils/query.py` | 查询工具（tables/kpi/daily/sql） |
| `ORACLE_SCHEMA.md` | Oracle 探查报告（27 张表详细字段） |
| `SETUP_REPORT.md` | 环境安装报告 |
| `OPERATIONS.md` | 运维手册 |
| `PROJECT_GUIDE.md` | 本文档（项目体系与使用指南） |
