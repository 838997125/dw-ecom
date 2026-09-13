-- ============================================
-- dim 层维度表 -- 从 raw 层清洗规范化
-- ============================================

-- 清除旧 dim 表
DROP TABLE IF EXISTS dim.product;
DROP TABLE IF EXISTS dim.customer;
DROP TABLE IF EXISTS dim.warehouse;
DROP TABLE IF EXISTS dim.supplier;
DROP TABLE IF EXISTS dim.staff;

-- ============================================
-- dim.product -- 商品维度表
-- 从 GOODSDOC + GOODSATTR 关键字段清洗而来
-- ============================================
CREATE TABLE dim.product AS
SELECT
    g.GOODSID AS product_id,
    g.GOODSCODE AS product_code,
    g.GOODSNAME AS product_name,
    g.SHORTNAME AS short_name,
    g.GOODSSPEC AS specification,
    g.MANUFACTURER AS manufacturer,
    g.PLACE AS origin,
    g.BARCODE AS barcode,
    g.UNITS AS unit,
    g.ISFREEZE AS is_frozen,
    g.ISABANDON AS is_abandoned,
    g.BEACTIVE AS is_active,
    g.K_SPBH AS erp_product_no,
    g.B2C_CODE AS ecommerce_code,
    TRY_CAST(g.CREATETIME AS TIMESTAMP) AS create_time,
    TRY_CAST(g.LASTMODIFYTIME AS TIMESTAMP) AS update_time,
    -- 业务标签
    CASE
        WHEN g.ISFREEZE = 'Y' THEN 'FROZEN'
        WHEN g.ISABANDON = 'Y' THEN 'ABANDONED'
        WHEN g.BEACTIVE = 'Y' THEN 'ACTIVE'
        ELSE 'INACTIVE'
    END AS status
FROM raw.GOODSDOC g;

-- ============================================
-- dim.customer -- 客户维度表
-- ============================================
CREATE TABLE dim.customer AS
SELECT
    c.CLIENTID AS customer_id,
    c.CLIENTCODE AS customer_code,
    c.CLIENTTYPE AS customer_type,
    c.ISSALE AS is_sales_customer,
    c.BEACTIVE AS is_active,
    c.ISABANDON AS is_abandoned,
    c.ELECCODE AS electronic_code,
    c.SALEMANID AS sales_person_id,
    c.DEPTID AS dept_id,
    TRY_CAST(c.CREATETIME AS TIMESTAMP) AS create_time,
    TRY_CAST(c.LASTMODIFYTIME AS TIMESTAMP) AS update_time,
    CASE
        WHEN c.ISABANDON = 'Y' THEN 'ABANDONED'
        WHEN c.BEACTIVE = 'Y' THEN 'ACTIVE'
        ELSE 'INACTIVE'
    END AS status
FROM raw.CLIENTDOC c;

-- ============================================
-- dim.warehouse -- 仓库维度表
-- ============================================
CREATE TABLE dim.warehouse AS
SELECT
    s.WHID AS warehouse_id,
    s.WHCODE AS warehouse_code,
    s.WHNAME AS warehouse_name,
    s.WHTYPE AS warehouse_type,
    s.BEACTIVE AS is_active,
    s.WHLEVEL AS warehouse_level,
    TRY_CAST(s.CREATETIME AS TIMESTAMP) AS create_time,
    TRY_CAST(s.LASTMODIFYTIME AS TIMESTAMP) AS update_time,
    CASE
        WHEN s.BEACTIVE = 'Y' THEN 'ACTIVE'
        ELSE 'INACTIVE'
    END AS status
FROM raw.STOREHOUSE s;

-- ============================================
-- dim.supplier -- 供应商维度表
-- ============================================
CREATE TABLE dim.supplier AS
SELECT
    s.SUPPLIERSID AS supplier_id,
    s.GYSBH AS supplier_code,
    s.SUPPCATE AS supplier_category,
    s.SUPPLEV AS supplier_level,
    s.SUPPTYPE AS supplier_type,
    s.BEACTIVE AS is_active,
    s.ISABANDON AS is_abandoned,
    TRY_CAST(s.CREATETIME AS TIMESTAMP) AS create_time,
    TRY_CAST(s.LASTMODIFYTIME AS TIMESTAMP) AS update_time,
    CASE
        WHEN s.ISABANDON = 'Y' THEN 'ABANDONED'
        WHEN s.BEACTIVE = 'Y' THEN 'ACTIVE'
        ELSE 'INACTIVE'
    END AS status
FROM raw.SUPPLYDOC s;

-- ============================================
-- dim.staff -- 员工维度表
-- ============================================
CREATE TABLE dim.staff AS
SELECT
    s.STAFFID AS staff_id,
    s.STAFFNAME AS staff_name,
    s.POSITIONS AS position,
    s.ISSALEMAN AS is_sales_person,
    s.ISPROCURMAN AS is_procurement_person,
    s.ISWARE AS is_warehouse_staff,
    s.BEACTIVE AS is_active,
    s.GENDER AS gender,
    TRY_CAST(s.CREATETIME AS TIMESTAMP) AS create_time,
    TRY_CAST(s.LASTMODIFYTIME AS TIMESTAMP) AS update_time,
    CASE
        WHEN s.BEACTIVE = 'Y' THEN 'ACTIVE'
        ELSE 'INACTIVE'
    END AS status
FROM raw.STAFFDOC s;

-- 建索引加速 JOIN
CREATE INDEX IF NOT EXISTS idx_dim_product_id ON dim.product(product_id);
CREATE INDEX IF NOT EXISTS idx_dim_customer_id ON dim.customer(customer_id);
CREATE INDEX IF NOT EXISTS idx_dim_warehouse_id ON dim.warehouse(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_raw_saleoutmt_billno ON raw.SALEOUTMT(BILLNO);
CREATE INDEX IF NOT EXISTS idx_raw_saleoutdt_billno ON raw.SALEOUTDT(BILLNO);
CREATE INDEX IF NOT EXISTS idx_raw_saleoutdt_goodsid ON raw.SALEOUTDT(GOODSID);

-- 统计
SELECT 'dim.product' AS tbl, COUNT(*) AS rows FROM dim.product
UNION ALL SELECT 'dim.customer', COUNT(*) FROM dim.customer
UNION ALL SELECT 'dim.warehouse', COUNT(*) FROM dim.warehouse
UNION ALL SELECT 'dim.supplier', COUNT(*) FROM dim.supplier
UNION ALL SELECT 'dim.staff', COUNT(*) FROM dim.staff;
