# 医药电商数仓 -- Schema 词典

> 本文件供 OpenClaw 理解数据结构用。自然语言查数时，根据此文件将问题翻译成 SQL。

## 连接信息

```bash
# DuckDB CLI
~/.local/bin/duckdb ~/.openclaw/workspace/dw-ecom/data/duckdb/ecom.duckdb

# Python 查询脚本
cd ~/.openclaw/workspace/dw-ecom && python3 -m src.utils.query sql "你的SQL"
```

## 表关系图

```
dim.product (商品维度)
  product_id = raw.SALEOUTDT.GOODSID
  product_id = raw.SALEOUTDT.GOODSID

dim.customer (客户维度)
  customer_id = raw.SALEOUTMT.CLIENTID

dim.warehouse (仓库维度)
  warehouse_id = raw.SALEOUTDT.WHID

raw.SALEOUTMT (销售出库主表)
  BILLNO = raw.SALEOUTDT.BILLNO (一对多)

raw.SALENOTESMT (销售单主表)
  BILLNO = raw.SALENOTESDT.BILLNO (一对多)

raw.PURORDERMT (采购订单主表)
  BILLNO = raw.PURORDERDT.BILLNO (一对多)

raw.PURINMT (采购入库主表)
  BILLNO = raw.PURINDT.BILLNO (一对多)
```

## 核心表字段速查

### raw.SALEOUTMT -- 销售出库主表 (65.6万行)

| 字段 | 类型 | 业务含义 |
|------|------|----------|
| BILLNO | BIGINT | 单据号(主键) |
| BILLCODE | VARCHAR | 单据编号 |
| DATES | VARCHAR | 业务日期 (YYYY-MM-DD) |
| ONTIME | VARCHAR | 业务时间 |
| SYSDATES | VARCHAR | 系统时间 |
| CLIENTID | VARCHAR | 客户ID |
| SALEMANID | VARCHAR | 业务员ID |
| AMOUNT | DECIMAL | 不含税金额 |
| TAX | DECIMAL | 税额 |
| TAXAMOUNT | DECIMAL | **含税金额（=销售额）** |
| PROFIT | DECIMAL | **毛利** |
| COSTAMT | DECIMAL | 成本金额 |
| RETAILAMT | DECIMAL | 零售额 |
| ISDONE | VARCHAR | 是否完成 (Y/N) |
| K_KDGS | VARCHAR | 快递公司 |
| K_KDDH | VARCHAR | 快递单号 |
| K_PROVINCE | VARCHAR | 省份 |
| K_CITY | VARCHAR | 城市 |
| REFOID | VARCHAR | 电商平台订单号 |
| BILLSTATE | DECIMAL | 单据状态 |

### raw.SALEOUTDT -- 销售出库明细 (84.4万行)

| 字段 | 类型 | 业务含义 |
|------|------|----------|
| BILLNO | BIGINT | 主表单据号(外键) |
| BILLSN | DECIMAL | 行号 |
| GOODSID | VARCHAR | 商品ID |
| WHID | VARCHAR | 仓库ID |
| NUM | DECIMAL | **数量** |
| PRICE | DECIMAL | 单价 |
| AMOUNT | DECIMAL | **金额** |
| TAXAMOUNT | DECIMAL | 含税金额 |
| COSTAMT | DECIMAL | 成本 |
| PROFIT | DECIMAL | 毛利 |
| BATCHCODE | VARCHAR | 批号 |
| VALDATE | VARCHAR | 有效期 |
| PRODUCEDATE | VARCHAR | 生产日期 |

### dim.product -- 商品维度 (5.1万行)

| 字段 | 类型 | 业务含义 |
|------|------|----------|
| product_id | VARCHAR | 商品ID (主键) |
| product_code | VARCHAR | 商品编码 |
| product_name | VARCHAR | **商品名称** |
| short_name | VARCHAR | 简称 |
| specification | VARCHAR | 规格 |
| manufacturer | VARCHAR | **生产厂家** |
| barcode | VARCHAR | 条形码 |
| unit | VARCHAR | 单位 |
| status | VARCHAR | 状态 (ACTIVE/INACTIVE/FROZEN/ABANDONED) |

### dim.customer -- 客户维度 (1.5万行)

| 字段 | 类型 | 业务含义 |
|------|------|----------|
| customer_id | VARCHAR | 客户ID (主键) |
| customer_code | VARCHAR | 客户编码 |
| customer_type | VARCHAR | 客户类型 |
| status | VARCHAR | 状态 (ACTIVE/INACTIVE/ABANDONED) |

### dim.warehouse -- 仓库维度 (9行)

| 字段 | 类型 | 业务含义 |
|------|------|----------|
| warehouse_id | VARCHAR | 仓库ID |
| warehouse_code | VARCHAR | 仓库编码 |
| warehouse_name | VARCHAR | **仓库名称** |
| status | VARCHAR | 状态 |

### raw.SALENOTESMT -- 销售单主表 (66.5万行)
字段含义同 SALEOUTMT，销售单是接单环节，出库是发货环节

### raw.BATCHCODE -- 批次批号 (61.5万行)

| 字段 | 业务含义 |
|------|----------|
| GOODSID | 商品ID |
| BATCHCODE | 批号 |
| PRODUCEDATE | 生产日期 |
| VALDATE | **有效期** |
| PURP | 采购价 |
| SALEP | 销售价 |
| RETAILP | 零售价 |
| SUPPLIERSID | 供应商ID |

### raw.ANGLEBALANCE -- 库位库存 (2.9万行)

| 字段 | 业务含义 |
|------|----------|
| GOODSID | 商品ID |
| PLACENUM | **当前库存数量** |
| STORMAX | 最大库存 |
| STORMIN | **最小库存(预警阈值)** |
| STORFIT | 合理库存 |

### raw.K_D3OMS_ORDERREFUNDMT -- 退款主表 (2300行)

| 字段 | 业务含义 |
|------|----------|
| ERP_ORDER_ID | ERP订单ID |
| REFUNDFEE | **退款金额** |
| STATUS | 状态 |
| REASON | 退款原因 |

### raw.PURORDERMT -- 采购订单主表 (4171行)
### raw.PURINMT -- 采购入库主表 (4650行)
字段含义类似销售表，SUPPLIERSID = 供应商ID

## 业务术语映射

| 业务术语 | SQL 对应 |
|----------|----------|
| 销售额 | `SUM(raw.SALEOUTMT.TAXAMOUNT)` |
| 不含税销售额 | `SUM(raw.SALEOUTMT.AMOUNT)` |
| 毛利 | `SUM(raw.SALEOUTMT.PROFIT)` |
| 毛利率 | `SUM(PROFIT) / SUM(TAXAMOUNT) * 100` |
| 销售成本 | `SUM(raw.SALEOUTMT.COSTAMT)` |
| 订单数 | `COUNT(DISTINCT raw.SALEOUTMT.BILLNO)` |
| 销售数量 | `SUM(raw.SALEOUTDT.NUM)` |
| 退款金额 | `SUM(raw.K_D3OMS_ORDERREFUNDMT.REFUNDFEE)` |
| 库存 | `raw.ANGLEBALANCE.PLACENUM` |
| 低库存 | `PLACENUM < STORMIN AND STORMIN > 0` |
| 今日 | `TRY_CAST(DATES AS DATE) = CURRENT_DATE` |
| 本周 | `TRY_CAST(DATES AS DATE) >= CURRENT_DATE - 6` |
| 本月 | `TRY_CAST(DATES AS DATE) >= DATE_TRUNC('month', CURRENT_DATE)` |
| 上月 | `TRY_CAST(DATES AS DATE) >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month') AND TRY_CAST(DATES AS DATE) < DATE_TRUNC('month', CURRENT_DATE)` |
| 昨天 | `TRY_CAST(DATES AS DATE) = CURRENT_DATE - 1` |
| 同比 | 与去年同期对比 |
| 环比 | 与上月对比 |

## 查询安全规则

1. **只允许 SELECT**，禁止 INSERT/UPDATE/DELETE/DROP/ALTER
2. **结果限制 1000 行**，自动加 LIMIT
3. **超时 10 秒**
4. **敏感字段不返回**：STAFFDOC.MOBILE, STAFFDOC.IDCARD 等
5. **时间字段是 CHAR 类型**，必须用 TRY_CAST 转换

## 常用查询模板

### 今日 KPI
```sql
SELECT COUNT(*) as 单数, SUM(TAXAMOUNT) as 含税销售额, SUM(PROFIT) as 毛利
FROM raw.SALEOUTMT
WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE
```

### 今日销售 TOP 20 商品
```sql
SELECT p.product_name, SUM(d.NUM) as 数量, SUM(d.AMOUNT) as 金额
FROM raw.SALEOUTDT d
JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
JOIN dim.product p ON d.GOODSID = p.product_id
WHERE TRY_CAST(m.DATES AS DATE) = CURRENT_DATE
GROUP BY p.product_name
ORDER BY 金额 DESC
LIMIT 20
```

### 按仓库汇总今日销售
```sql
SELECT w.warehouse_name, COUNT(m.BILLNO) as 单数, SUM(d.AMOUNT) as 金额
FROM raw.SALEOUTMT m
JOIN raw.SALEOUTDT d ON m.BILLNO = d.BILLNO
JOIN dim.warehouse w ON d.WHID = w.warehouse_id
WHERE TRY_CAST(m.DATES AS DATE) = CURRENT_DATE
GROUP BY w.warehouse_name
ORDER BY 金额 DESC
```

### 低库存预警
```sql
SELECT p.product_name, a.PLACENUM as 当前库存, a.STORMIN as 预警阈值
FROM raw.ANGLEBALANCE a
JOIN dim.product p ON a.GOODSID = p.product_id
WHERE a.PLACENUM < a.STORMIN AND a.STORMIN > 0
ORDER BY a.PLACENUM ASC
```

### 近效期商品 (90天内过期)
```sql
SELECT p.product_name, b.BATCHCODE as 批号, b.VALDATE as 有效期
FROM raw.BATCHCODE b
JOIN dim.product p ON b.GOODSID = p.product_id
WHERE TRY_CAST(b.VALDATE AS DATE) <= CURRENT_DATE + 90
  AND TRY_CAST(b.VALDATE AS DATE) >= CURRENT_DATE
ORDER BY b.VALDATE ASC
```

### 近7天销售趋势
```sql
SELECT TRY_CAST(m.DATES AS DATE) as 日期,
       COUNT(*) as 单数,
       SUM(m.TAXAMOUNT) as 含税销售额,
       SUM(m.PROFIT) as 毛利
FROM raw.SALEOUTMT m
WHERE TRY_CAST(m.DATES AS DATE) >= CURRENT_DATE - 6
GROUP BY 1
ORDER BY 1
```

### 退款汇总
```sql
SELECT COUNT(*) as 退款单数, SUM(REFUNDFEE) as 退款金额
FROM raw.K_D3OMS_ORDERREFUNDMT
WHERE TRY_CAST(LASTMODIFYTIME AS DATE) = CURRENT_DATE
```
