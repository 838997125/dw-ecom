#!/usr/bin/env python3
"""
Refresh agg.c_retail_detail (C端零售汇总底表)

Full rebuild + trace code aggregation. Takes ~5 seconds.
Includes retry logic to handle lock conflicts with MCP queries and CDC sync.

Lock behavior (DuckDB single-writer model):
  - MCP server uses short-lived read-only connections (connect→query→close, ms-level)
  - This script uses one read-write connection for ~5s
  - If a read is in progress when this connects, it retries (up to 6×5s = 30s)
  - If this is writing when MCP queries, MCP retries (up to 6×2s = 12s)
  - No deadlock possible; worst case is a brief wait
"""
import duckdb
import time
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 优先环境变量 DUCKDB_PATH，否则用项目内相对路径（本文件位于 <ROOT>/src/agg/）
DB_PATH = os.environ.get(
    'DUCKDB_PATH',
    str(Path(__file__).resolve().parents[2] / 'data' / 'duckdb' / 'ecom.duckdb')
)
MAX_RETRIES = 6
RETRY_WAIT = 5  # seconds

SQL_REBUILD = """
CREATE OR REPLACE TABLE agg.c_retail_detail AS
SELECT
    b.billno,
    b.entid,
    b.billcode,
    b.ruleid,
    b.dates AS sale_date,
    b.ontime AS sale_time,
    b.clientid,
    b.saleclientid,
    b.salemanid,
    b.orgid,
    c.clientcode,
    f.businessname,
    a.billsn,
    a.goodsid,
    a.packid,
    a.meas,
    a.unit,
    a.basenum,
    a.rate,
    a.num,
    a.remark,
    d.goodscode,
    d.goodsname,
    d.goodsspec,
    d.place,
    d.manufacturer,
    g.whname,
    CAST(NULL AS VARCHAR) AS locatname,
    a.ownerid,
    a.whorgid,
    a.whid,
    a.locatid,
    m.batchcode,
    m.producedate,
    m.valdate,
    CAST(NULL AS VARCHAR) AS sterilcode,
    CAST(NULL AS VARCHAR) AS orgname,
    j.staffname AS salemanname,
    l.staffname AS caozyname,
    j.staffname AS staffname1,
    CASE
        WHEN d3.remark LIKE '%金额0补发%' THEN 0
        WHEN a.ds_price = 0 THEN a.taxprice
        ELSE a.ds_price
    END AS ds_price,
    CASE
        WHEN d3.remark LIKE '%金额0补发%' THEN 0
        WHEN a.ds_price = 0 THEN a.taxprice
        ELSE a.ds_price
    END * a.num AS ds_amount,
    CASE
        WHEN d3.remark LIKE '%金额0补发%' THEN 0
        WHEN a.ds_price = 0 THEN a.taxprice
        ELSE a.ds_price
    END * a.num AS ys_amount,
    CASE
        WHEN d3.remark LIKE '%金额0补发%' THEN 0
        WHEN a.ds_price = 0 THEN a.taxprice
        ELSE a.ds_price
    END * a.num - COALESCE(m.purtaxp, 0) * a.num AS taxprofit,
    a.costamt,
    COALESCE(m.purtaxp, 0) * a.num AS taxcostamt,
    CASE
        WHEN d3.remark LIKE '%金额0补发%' THEN 0
        ELSE ROUND(
            (CASE WHEN a.ds_price = 0 THEN a.taxprice ELSE a.ds_price END * a.num)
            / (100 + a.rate) * 100, 2
        )
    END AS net_amount,
    ROUND(
        (CASE WHEN d3.remark LIKE '%金额0补发%' THEN 0
              WHEN a.ds_price = 0 THEN a.taxprice
              ELSE a.ds_price END * a.num)
        / (100 + a.rate) * 100, 2
    ) - a.costamt AS net_profit,
    CASE
        WHEN (CASE WHEN a.ds_price = 0 THEN a.taxprice ELSE a.ds_price END * a.num) = 0 THEN 0
        ELSE (
            (CASE WHEN d3.remark LIKE '%金额0补发%' THEN 0
                  WHEN a.ds_price = 0 THEN a.taxprice
                  ELSE a.ds_price END * a.num) - a.taxamount
        ) / (CASE WHEN a.ds_price = 0 THEN a.taxprice ELSE a.ds_price END * a.num)
    END AS profitrate,
    b.posname,
    b.orderid,
    b.b2bcode AS platform_order_no,
    b.refoid AS so_id,
    COALESCE(NULLIF(CAST(b.logisticsno AS VARCHAR), ''), NULLIF(d3.expresscode, '')) AS logisticsno,
    ga.k_xslb,
    CAST(NULL AS VARCHAR) AS b2c_fl,
    CAST(NULL AS VARCHAR) AS b2c_cgy,
    CAST(NULL AS VARCHAR) AS b2c_beian,
    ga.onecatname,
    ga.recipetype,
    ga.generalname,
    CAST(NULL AS VARCHAR) AS agg_trace_codes,
    CASE WHEN b.billcode LIKE 'WOT%' THEN 'sale' ELSE 'return' END AS bill_type,
    CURRENT_TIMESTAMP AS _etl_time
FROM raw.SaleOutDt a
JOIN raw.SaleOutMt b ON a.billno = b.billno AND a.entid = b.entid
LEFT JOIN raw.ClientDoc c ON b.clientid = c.clientid AND b.entid = c.entid
LEFT JOIN raw.BusinessDoc f ON c.clientid = f.businessid AND c.entid = f.entid
LEFT JOIN raw.GoodsDoc d ON a.goodsid = d.goodsid AND a.entid = d.entid
LEFT JOIN raw.GoodsAttr ga ON d.goodsid = ga.goodsid AND d.entid = ga.entid
LEFT JOIN raw.StoreHouse g ON g.whid = a.whid AND a.entid = g.entid
LEFT JOIN raw.StaffDoc j ON b.salemanid = j.staffid AND b.entid = j.entid
LEFT JOIN raw.StaffDoc l ON b.caozy = l.staffid AND b.entid = l.entid
LEFT JOIN raw.BatchCode m ON a.angleid = m.angleid AND a.goodsid = m.goodsid AND a.entid = m.entid
LEFT JOIN raw.K_D3OMS_STOCKOUTMT_PLAN d3 ON b.orderid = d3.erp_order_id
WHERE b.ruleid IN ('36jjy6yj3e6vd5lj', '3nroqpch9j6bdggn')
  AND COALESCE(b.orderid, '') <> ''
"""

SQL_TRACE = """
UPDATE agg.c_retail_detail t
SET agg_trace_codes = sub.agg_trace_codes
FROM (
    SELECT
        a.BILLNO,
        STRING_AGG(DISTINCT w.IMEINO, ',' ORDER BY w.IMEINO) AS agg_trace_codes
    FROM raw.SALEOUTDT a
    JOIN raw.SALEOUTMT b ON a.BILLNO = b.BILLNO AND a.ENTID = b.ENTID
    JOIN raw.SKWMS_JGM_W w ON a.RFBILLNO = w.TRAN_BILLNO
        AND a.GOODSID = w.GOODSID
        AND w.TRAN_TYPE = 'XSCK'
    WHERE b.RULEID IN ('36jjy6yj3e6vd5lj', '3nroqpch9j6bdggn')
      AND a.RFBILLNO IS NOT NULL AND a.RFBILLNO > 0
    GROUP BY a.BILLNO
) sub
WHERE t.billno = sub.BILLNO
"""

SQL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_crd_date ON agg.c_retail_detail(sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_crd_billno ON agg.c_retail_detail(billno)",
    "CREATE INDEX IF NOT EXISTS idx_crd_posname ON agg.c_retail_detail(posname)",
    "CREATE INDEX IF NOT EXISTS idx_crd_goodsid ON agg.c_retail_detail(goodsid)",
    "CREATE INDEX IF NOT EXISTS idx_crd_goodscode ON agg.c_retail_detail(goodscode)",
    "CREATE INDEX IF NOT EXISTS idx_crd_orderid ON agg.c_retail_detail(orderid)",
    "CREATE INDEX IF NOT EXISTS idx_crd_billtype ON agg.c_retail_detail(bill_type)",
]


def connect_with_retry():
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return duckdb.connect(DB_PATH)
        except Exception as e:
            if 'lock' in str(e).lower() and attempt < MAX_RETRIES:
                print(f"  Lock conflict, retry {attempt}/{MAX_RETRIES} in {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
            else:
                raise


def main():
    t0 = time.time()
    ts = lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    print(f"[{ts()}] Connecting (with retry)...")
    con = connect_with_retry()

    # Step 1: Rebuild
    t1 = time.time()
    print(f"[{ts()}] Rebuilding c_retail_detail ...")
    con.execute(SQL_REBUILD)
    t2 = time.time()
    print(f"  CTAS done in {t2-t1:.1f}s")

    # Step 2: Trace codes
    print(f"[{ts()}] Aggregating trace codes ...")
    con.execute(SQL_TRACE)
    t3 = time.time()
    print(f"  Trace codes done in {t3-t2:.1f}s")

    # Step 3: Indexes
    for sql in SQL_INDEXES:
        con.execute(sql)
    t4 = time.time()
    print(f"  Indexes done in {t4-t3:.1f}s")

    # Step 4: 重建表会清掉 COMMENT，重新应用中文备注（幂等）
    try:
        import subprocess, sys as _sys
        script = str(Path(__file__).resolve().parents[2] / "scripts" / "apply_schema_comments.py")
        if Path(script).exists():
            print(f"[{ts()}] Re-applying schema comments ...")
            subprocess.run([_sys.executable, script, "--apply"],
                           capture_output=True, text=True, timeout=120,
                           cwd=str(Path(script).parents[1]))
            print(f"  Comments re-applied")
    except Exception as e:
        print(f"  [WARN] re-apply comments failed: {e}")

    # Stats
    stats = con.execute("""
        SELECT
            COUNT(*), COUNT(DISTINCT billno),
            MIN(sale_date), MAX(sale_date),
            SUM(CASE WHEN bill_type='sale' THEN 1 ELSE 0 END),
            SUM(CASE WHEN bill_type='return' THEN 1 ELSE 0 END),
            ROUND(SUM(CASE WHEN bill_type='sale' THEN ds_amount ELSE 0 END), 2),
            ROUND(SUM(CASE WHEN bill_type='return' THEN ds_amount ELSE 0 END), 2),
            SUM(CASE WHEN agg_trace_codes IS NOT NULL AND agg_trace_codes <> '' THEN 1 ELSE 0 END)
        FROM agg.c_retail_detail
    """).fetchone()
    con.close()

    elapsed = time.time() - t0
    print(f"[{ts()}] Done in {elapsed:.1f}s")
    print(f"  Rows: {stats[0]:,} | Bills: {stats[1]:,} | {stats[2]}~{stats[3]}")
    print(f"  Sale: {stats[4]:,} rows / {stats[6]:,.2f} | "
          f"Return: {stats[5]:,} rows / {stats[7]:,.2f}")
    print(f"  With trace codes: {stats[8]:,}")


if __name__ == '__main__':
    main()
