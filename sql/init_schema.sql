-- ============================================
-- 医药电商数据仓库 - DuckDB 三层结构初始化
-- ============================================

-- raw 层：原始数据（按业务表镜像）
CREATE SCHEMA IF NOT EXISTS raw;

-- agg 层：聚合结果
CREATE SCHEMA IF NOT EXISTS agg;

-- dim 层：维度表
CREATE SCHEMA IF NOT EXISTS dim;

-- ============ dim 层 ============
CREATE TABLE IF NOT EXISTS dim.customer (
    customer_id VARCHAR PRIMARY KEY,
    customer_name VARCHAR,
    customer_type VARCHAR,
    region VARCHAR,
    province VARCHAR,
    city VARCHAR,
    contact_phone VARCHAR,
    status VARCHAR,
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dim.product (
    product_id VARCHAR PRIMARY KEY,
    product_name VARCHAR,
    product_code VARCHAR,
    barcode VARCHAR,
    category_l1 VARCHAR,
    category_l2 VARCHAR,
    specification VARCHAR,
    manufacturer VARCHAR,
    unit VARCHAR,
    price_retail DECIMAL(12,2),
    price_purchase DECIMAL(12,2),
    status VARCHAR,
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dim.warehouse (
    warehouse_id VARCHAR PRIMARY KEY,
    warehouse_name VARCHAR,
    warehouse_type VARCHAR,
    location VARCHAR,
    status VARCHAR,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============ raw 层（示例：订单、库存、销售）============
-- 实际表结构待 Oracle 探查后补充
CREATE TABLE IF NOT EXISTS raw.orders (
    order_id VARCHAR,
    order_no VARCHAR,
    order_type VARCHAR,
    customer_id VARCHAR,
    warehouse_id VARCHAR,
    order_status VARCHAR,
    total_amount DECIMAL(14,2),
    total_qty DECIMAL(14,2),
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    pay_time TIMESTAMP,
    ship_time TIMESTAMP,
    complete_time TIMESTAMP,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw.order_lines (
    line_id VARCHAR,
    order_id VARCHAR,
    product_id VARCHAR,
    qty DECIMAL(14,2),
    unit_price DECIMAL(12,2),
    line_amount DECIMAL(14,2),
    discount DECIMAL(12,2),
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw.inventory (
    inventory_id VARCHAR,
    warehouse_id VARCHAR,
    product_id VARCHAR,
    qty_available DECIMAL(14,2),
    qty_locked DECIMAL(14,2),
    qty_in_transit DECIMAL(14,2),
    unit_cost DECIMAL(12,2),
    update_time TIMESTAMP,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============ agg 层 ============
CREATE TABLE IF NOT EXISTS agg.kpi_realtime (
    metric_date DATE,
    metric_hour INTEGER,
    metric_name VARCHAR,
    metric_value DECIMAL(14,2),
    metric_unit VARCHAR,
    yoy_value DECIMAL(14,2),       -- 同比
    mom_value DECIMAL(14,2),       -- 环比
    target_value DECIMAL(14,2),    -- 目标值
    achievement_rate DECIMAL(8,4), -- 完成率
    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agg.daily_sales (
    sale_date DATE,
    warehouse_id VARCHAR,
    customer_id VARCHAR,
    product_id VARCHAR,
    order_count INTEGER,
    total_qty DECIMAL(14,2),
    total_amount DECIMAL(14,2),
    total_cost DECIMAL(14,2),
    gross_profit DECIMAL(14,2),
    gross_margin DECIMAL(8,4),
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agg.alert_inventory (
    alert_date DATE,
    alert_time TIMESTAMP,
    warehouse_id VARCHAR,
    product_id VARCHAR,
    product_name VARCHAR,
    alert_type VARCHAR,      -- LOW_STOCK / OVERSTOCK / EXPIRY
    current_qty DECIMAL(14,2),
    threshold_qty DECIMAL(14,2),
    severity VARCHAR,        -- HIGH / MEDIUM / LOW
    status VARCHAR,          -- OPEN / RESOLVED / IGNORED
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agg.weekly_report (
    week_start DATE,
    week_end DATE,
    metric_name VARCHAR,
    metric_value DECIMAL(14,2),
    yoy_value DECIMAL(14,2),
    wow_value DECIMAL(14,2),  -- 周环比
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agg.monthly_report (
    month_start DATE,
    month_end DATE,
    metric_name VARCHAR,
    metric_value DECIMAL(14,2),
    yoy_value DECIMAL(14,2),
    mom_value DECIMAL(14,2),
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agg.data_audit (
    audit_date DATE,
    table_name VARCHAR,
    source_count BIGINT,
    target_count BIGINT,
    diff_count BIGINT,
    status VARCHAR,          -- OK / MISMATCH / ERROR
    error_detail VARCHAR,
    etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============ 元数据表 ============
CREATE TABLE IF NOT EXISTS raw._cdc_state (
    table_name VARCHAR PRIMARY KEY,
    last_cdc_time TIMESTAMP,
    last_record_count BIGINT,
    last_sync_time TIMESTAMP,
    status VARCHAR,
    error_msg VARCHAR
);

-- 完成
SELECT 'Schema initialized successfully' AS result;
