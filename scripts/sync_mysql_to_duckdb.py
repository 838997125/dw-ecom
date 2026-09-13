#!/usr/bin/env python3
"""
将 MySQL 中 YMSS_DING / YMSS_DWD / YMSS_DWS 的指定表同步到 DuckDB。
DuckDB 中使用同名 schema（小写）镜像 MySQL 数据库。
"""
import os
import sys
import time
from pathlib import Path

# 启动早期加载项目根 .env（MYSQL_PASSWORD 等）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from envloader import load_env
    load_env()
except Exception:
    pass

import pymysql
import duckdb
import pandas as pd

DB_PATH = os.environ.get(
    'DUCKDB_PATH',
    str(Path(__file__).resolve().parent.parent / "data" / "duckdb" / "ecom.duckdb")
)

# MySQL 连接信息从环境变量读取，不硬编码密码
#   MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD
MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "charset": "utf8mb4",
}

# (mysql_schema, mysql_table, duckdb_schema, duckdb_table)
TABLES = [
    # YMSS_DING
    ("YMSS_DING", "B2B_Report",                "ymss_ding", "b2b_report"),
    ("YMSS_DING", "ERP_Retail_Summary",        "ymss_ding", "erp_retail_summary"),
    ("YMSS_DING", "Single_Pharmacy_Report",    "ymss_ding", "single_pharmacy_report"),
    ("YMSS_DING", "ODS_每日全量销售流水",       "ymss_ding", "ods_daily_full_sales"),
    # YMSS_DWD
    ("YMSS_DWD", "DWD_每日商品销售明细宽表",     "ymss_dwd", "dwd_daily_product_sales"),
    ("YMSS_DWD", "DWD_无效运营销售明细宽表",     "ymss_dwd", "dwd_invalid_ops_sales"),
    ("YMSS_DWD", "DWD_有效运营销售明细宽表",     "ymss_dwd", "dwd_valid_ops_sales"),
    ("YMSS_DWD", "DWD_有效运营销售明细宽表_补充", "ymss_dwd", "dwd_valid_ops_sales_supp"),
    # YMSS_DWS
    ("YMSS_DWS", "ADS_月度店铺无效销售汇总表",    "ymss_dws", "ads_monthly_shop_invalid_sales"),
    ("YMSS_DWS", "ADS_月度店铺销售汇总表",        "ymss_dws", "ads_monthly_shop_sales"),
    ("YMSS_DWS", "ADS_月度项目销售达成表",        "ymss_dws", "ads_monthly_project_achievement"),
    ("YMSS_DWS", "ADS_月度运营系列达成表",        "ymss_dws", "ads_monthly_series_achievement"),
    ("YMSS_DWS", "DWS_每日店铺无效销售汇总",      "ymss_dws", "dws_daily_shop_invalid_sales"),
    ("YMSS_DWS", "DWS_每日店铺销售汇总",          "ymss_dws", "dws_daily_shop_sales"),
    ("YMSS_DWS", "DWS_每日项目销售汇总",          "ymss_dws", "dws_daily_project_sales"),
    ("YMSS_DWS", "DWS_每日运营系列销售汇总",      "ymss_dws", "dws_daily_series_sales"),
]


def sync_table(mysql_conn, duck_conn, mysql_schema, mysql_table, duck_schema, duck_table):
    t0 = time.time()
    print(f"  → {mysql_schema}.{mysql_table} → {duck_schema}.{duck_table} ...", end=" ", flush=True)

    # 读取 MySQL 数据
    sql = f"SELECT * FROM `{mysql_schema}`.`{mysql_table}`"
    df = pd.read_sql(sql, mysql_conn)

    # 创建 schema
    duck_conn.execute(f"CREATE SCHEMA IF NOT EXISTS {duck_schema}")

    # 先删后建（全量同步）
    duck_conn.execute(f'DROP TABLE IF EXISTS {duck_schema}."{duck_table}"')

    # 写入 DuckDB
    duck_conn.register("_tmp_df", df)
    duck_conn.execute(f'CREATE TABLE {duck_schema}."{duck_table}" AS SELECT * FROM _tmp_df')
    duck_conn.unregister("_tmp_df")

    row_count = len(df)
    elapsed = time.time() - t0
    print(f"✅ {row_count:,} rows, {elapsed:.1f}s")
    return row_count


def main():
    print(f"DuckDB: {DB_PATH}")
    print(f"Connecting to MySQL ...")
    mysql_conn = pymysql.connect(database=None, **MYSQL_CONFIG)
    duck_conn = duckdb.connect(DB_PATH)

    total_rows = 0
    for i, (ms, mt, ds, dt) in enumerate(TABLES, 1):
        print(f"[{i}/{len(TABLES)}]")
        try:
            n = sync_table(mysql_conn, duck_conn, ms, mt, ds, dt)
            total_rows += n
        except Exception as e:
            print(f"  ❌ FAILED: {e}")

    duck_conn.close()
    mysql_conn.close()
    print(f"\n{'='*60}")
    print(f"同步完成，共 {total_rows:,} 行，{len(TABLES)} 张表")


if __name__ == "__main__":
    main()
