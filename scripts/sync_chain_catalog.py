#!/usr/bin/env python3
"""
sync_chain_catalog.py - 连锁目录清单全量刷新

从时空 Oracle 直接执行连锁目录清单 SQL，全量刷新 DuckDB raw.chain_catalog 表。
数据来源：Oracle RACE schema（实时查询，不依赖 DuckDB raw 层中间表）。

定时建议：每天早上 7:00 执行一次（在 CDC 增量同步之后）。

用法：
  python3 scripts/sync_chain_catalog.py
  python3 scripts/sync_chain_catalog.py --entid E4KVKGVOXNV   # 指定 entid
  python3 scripts/sync_chain_catalog.py --search "%阿莫西林%"   # 指定搜索条件
"""
import sys
sys.path.insert(0, ".")
from src.utils.db import resolve_secret
import os
import time
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import OracleConn, DuckDBConn
import yaml
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / 'logs' / 'chain_catalog.log', encoding='utf-8')
    ]
)
logger = logging.getLogger('chain_catalog')

# 连锁目录清单 SQL（基于用户提供的 SQL，适配参数化）
# 注意：readonly_user 默认 schema 不是 RACE，所有表必须带 RACE. 前缀
# :entid 替换为具体值，:vzjm 替换为 '%%'（全量，不筛选）
CHAIN_CATALOG_SQL = """
WITH MD_XSSL AS (
    SELECT a.GoodsId, SUM(a.num_30d) AS num_30d, SUM(a.num_60d) AS num_60d, SUM(a.num_90d) AS num_90d, g.entid
    FROM RACE.K_LSXS_DT a
    JOIN RACE.goodsdoc g ON a.goodsid = g.goodsid AND g.entid = :entid
    WHERE a.orgid NOT IN ('O4PLWZR3M9E', 'O2LRUYP6ZNF')
    AND (UPPER(g.GoodsCode) LIKE :vzjm OR UPPER(g.GoodsName) LIKE :vzjm
         OR g.Logogram LIKE :vzjm OR g.Manufacturer LIKE :vzjm)
    GROUP BY a.GoodsId, g.entid
),
MD_KC AS (
    SELECT a.GoodsId, SUM(a.Num) AS MDPlaceNum, g.entid
    FROM RACE.K_LSKC_DT a
    JOIN RACE.goodsdoc g ON a.goodsid = g.goodsid AND g.entid = :entid
    WHERE a.ownerid NOT IN ('O4PLWZR3M9E', 'O2LRUYP6ZNF')
    AND (UPPER(g.GoodsCode) LIKE :vzjm OR UPPER(g.GoodsName) LIKE :vzjm
         OR g.Logogram LIKE :vzjm OR g.Manufacturer LIKE :vzjm)
    GROUP BY a.GoodsId, g.entid
),
ZB_ZHJJ AS (
    SELECT c.entid, d.goodsid, f.businesscode, f.businessname, e.dates,
           c.zdjj, s.contact AS OppContact,
           SUM(d.basenum) AS innum, MAX(d.taxprice) AS taxprice
    FROM (
        SELECT a.entid, b.goodsid, MAX(b.billno) AS billno, MIN(b.taxprice) AS zdjj
        FROM RACE.purinmt a
        JOIN RACE.purindt b ON a.billno = b.billno AND a.entid = b.entid AND a.entid = :entid
        WHERE a.RuleId = '12ealvx84t7bl04x'
        AND a.Dates >= TO_CHAR(SYSDATE - 365, 'YYYY-MM-DD')
        GROUP BY a.entid, b.goodsid
    ) c
    JOIN RACE.purindt d ON c.billno = d.billno AND c.goodsid = d.goodsid AND c.entid = d.entid
    JOIN RACE.PURINMT e ON d.billno = e.billno AND d.entid = e.entid
    JOIN RACE.businessdoc f ON e.suppliersid = f.businessid AND e.entid = f.entid
    JOIN RACE.goodsdoc g ON d.goodsid = g.goodsid AND g.entid = d.entid
    LEFT JOIN RACE.contactdoc s ON e.OppContId = s.contactid AND e.entid = s.entid
    WHERE 1=1
    AND (UPPER(g.GoodsCode) LIKE :vzjm OR UPPER(g.GoodsName) LIKE :vzjm
         OR g.Logogram LIKE :vzjm OR g.Manufacturer LIKE :vzjm)
    GROUP BY c.entid, d.goodsid, f.businesscode, f.businessname, e.dates, c.zdjj, s.contact
),
ZB_ZTNUM AS (
    SELECT b.entid, b.goodsid, SUM(b.num - b.execnum) AS ztnum
    FROM RACE.purordermt a
    JOIN RACE.purorderdt b ON a.billno = b.billno AND a.entid = b.entid
    JOIN RACE.goodsdoc g ON b.goodsid = g.goodsid AND b.entid = g.entid AND g.entid = :entid
    WHERE a.entid = :entid AND a.isend = 'Y' AND a.isdone = 'N'
    AND a.billstate = 0 AND b.num - b.execnum > 0
    AND (UPPER(g.GoodsCode) LIKE :vzjm OR UPPER(g.GoodsName) LIKE :vzjm
         OR g.Logogram LIKE :vzjm OR g.Manufacturer LIKE :vzjm)
    GROUP BY b.entid, b.goodsid
),
xs_zzl AS (
    SELECT a.entid, b.goodsid, c.stornum, SUM(b.num) AS xs_num,
           COUNT(DISTINCT SUBSTR(a.dates, 1, 7)) AS ts,
           FLOOR((c.stornum /
               CASE WHEN NVL(SUM(b.num), 0) = 0 THEN 1
                    ELSE (SUM(b.num) / COUNT(DISTINCT SUBSTR(a.dates, 1, 7))) END
           ) * 30) AS zz_ts
    FROM RACE.saleoutmt a
    JOIN RACE.saleoutdt b ON a.entid = b.entid AND a.billno = b.billno
    JOIN (
        SELECT entid, goodsid, whid, SUM(stornum) AS stornum
        FROM RACE.storbalance WHERE stornum <> 0
        GROUP BY entid, goodsid, whid
    ) c ON b.goodsid = c.goodsid AND b.entid = c.entid AND c.whid = 'K4MN5GEA9Z7'
    WHERE a.ruleid <> '3cc08h3z7cnkrv2q'
    AND a.dates > TO_CHAR(ADD_MONTHS(SYSDATE, -24), 'YYYY-MM-DD')
    GROUP BY a.entid, b.goodsid, c.stornum
),
ps_shl AS (
    SELECT a.entid, b.goodsid, SUM(b.num) AS ps_num
    FROM RACE.saleoutmt a
    JOIN RACE.saleoutdt b ON a.entid = b.entid AND a.billno = b.billno
    WHERE a.ruleid IN ('36jjy6yj3e6vd5lj', '3nroqpch9j6bdggn')
    AND a.dates >= TO_CHAR(SYSDATE - 30, 'YYYY-MM-DD')
    AND b.whid = 'K4MN5GEA9Z7'
    GROUP BY a.entid, b.goodsid
),
qhd_quehuo AS (
    SELECT a.goodsid, SUM(a.num - NVL(a.execnum, 0)) AS Q_Num
    FROM RACE.Requestdt a
    JOIN RACE.RequestMt b ON a.billno = b.billno AND a.entid = b.entid
    WHERE b.dates >= TO_CHAR(SYSDATE - 7, 'YYYY-MM-DD')
    AND b.isdone = 'Y'
    GROUP BY a.goodsid
    HAVING SUM(a.num - NVL(a.execnum, 0)) > 0
)
SELECT DISTINCT
    a.entid, a.GoodsId, a.GoodsCode, a.BarCode, a.GoodsName, b.GeneralName,
    a.GoodsSpec, a.Manufacturer, a.isfreeze, a.B2C_cgy, a.is_type, b.IsProcur,
    b.issale, b.Formula, b.ApprovalNo, b.ApprovalTo, b.MedCareType, b.BiddPrice,
    b.bzjs, b.k_xslb, b.onecatname, b.twocatname, b.k_isqh, b.threecatname,
    b.OutTaxPrice, b.RetailP, b.MedCareCode, b.k_cgsx, f.businessname,
    d.MDPlaceNum, p.unit, NVL(c.placenum, 0) AS PlaceNum, f.dates AS Last_Date,
    NVL(f.taxprice, 0) AS taxprice, NVL(f.zdjj, 0) AS zdjj, NVL(f.innum, 0) AS innum,
    f.OppContact, z.ztnum, NVL(ps.ps_num, 0) AS ps_num,
    NVL(e.num_30d, 0) AS num_30d, NVL(e.num_60d, 0) AS num_60d,
    NVL(e.num_90d, 0) AS num_90d, zz.zz_ts, b.k_gwj, b.fourcatname,
    b.fivecatname, NVL(qh.Q_Num, 0) AS Q_Num, NVL(cc.placenum_bc, 0) AS placenum_bc
FROM RACE.GOODSDOC a
JOIN RACE.GOODSATTR b ON a.EntId = b.EntId AND a.GoodsId = b.GoodsId
LEFT JOIN (
    SELECT EntId, GoodsId, SUM(BeSaleNum) AS placenum FROM RACE.ZBKXKC GROUP BY EntId, GoodsId
) c ON a.EntId = c.EntId AND a.GoodsId = c.GoodsId
LEFT JOIN (
    SELECT x.EntId, x.GoodsId, SUM(x.placenum - NVL(y.basenum, 0)) AS placenum_bc
    FROM RACE.storbalance x
    LEFT JOIN (
        SELECT entid, whid, goodsid, SUM(basenum) AS basenum
        FROM RACE.goodsoccu GROUP BY entid, whid, goodsid
    ) y ON x.entid = y.entid AND x.whid = y.whid AND x.goodsid = y.goodsid
    WHERE x.whid IN ('K4SM9IU5OTP', 'K4S1C37LBLL')
    GROUP BY x.EntId, x.GoodsId
) cc ON a.EntId = cc.EntId AND a.GoodsId = cc.GoodsId
LEFT JOIN MD_KC d ON a.GoodsId = d.goodsid AND a.entid = d.entid
LEFT JOIN MD_XSSL e ON a.GoodsId = e.GoodsId AND a.entid = e.entid
LEFT JOIN ZB_ZHJJ f ON a.GoodsId = f.GoodsId AND a.entid = f.entid
LEFT JOIN RACE.pgprice p ON a.goodsid = p.goodsid AND a.entid = p.entid AND p.isbase = 'Y'
LEFT JOIN ZB_ZTNUM z ON a.goodsid = z.goodsid AND a.entid = z.entid
LEFT JOIN xs_zzl zz ON b.goodsid = zz.goodsid AND b.entid = zz.entid
LEFT JOIN ps_shl ps ON b.goodsid = ps.goodsid AND b.entid = ps.entid
LEFT JOIN qhd_quehuo qh ON a.goodsid = qh.goodsid
WHERE a.beactive = 'Y' AND b.beactive = 'Y' AND a.entid = :entid
AND (UPPER(a.GoodsCode) LIKE :vzjm OR UPPER(a.GoodsName) LIKE :vzjm
     OR a.Logogram LIKE :vzjm OR b.GeneralName LIKE :vzjm
     OR a.Manufacturer LIKE :vzjm)
"""

# DuckDB 目标表 DDL
DDL_CHAIN_CATALOG = """
CREATE OR REPLACE TABLE raw.chain_catalog (
    entid VARCHAR,
    goodsid VARCHAR,
    goods_code VARCHAR,
    barcode VARCHAR,
    goods_name VARCHAR,
    general_name VARCHAR,
    goods_spec VARCHAR,
    manufacturer VARCHAR,
    is_freeze VARCHAR,
    b2c_cgy VARCHAR,
    is_type VARCHAR,
    is_procur VARCHAR,
    is_sale VARCHAR,
    formula VARCHAR,
    approval_no VARCHAR,
    approval_to VARCHAR,
    med_care_type VARCHAR,
    bidd_price DOUBLE,
    bzjs DOUBLE,
    k_xslb VARCHAR,
    cat_level1 VARCHAR,
    cat_level2 VARCHAR,
    k_isqh VARCHAR,
    cat_level3 VARCHAR,
    out_tax_price DOUBLE,
    retail_price DOUBLE,
    med_care_code VARCHAR,
    k_cgsx VARCHAR,
    inbound_supplier VARCHAR,
    md_place_num DOUBLE,
    unit VARCHAR,
    place_num DOUBLE,
    last_inbound_date VARCHAR,
    tax_price DOUBLE,
    min_purchase_price DOUBLE,
    inbound_qty DOUBLE,
    supplier_contact VARCHAR,
    in_transit_qty DOUBLE,
    delivery_30d DOUBLE,
    store_sales_30d DOUBLE,
    store_sales_60d DOUBLE,
    store_sales_90d DOUBLE,
    turnover_days DOUBLE,
    k_gwj DOUBLE,
    cat_level4 VARCHAR,
    cat_level5 VARCHAR,
    shortage_7d DOUBLE,
    bc_place_num DOUBLE,
    _etl_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def load_config():
    with open(ROOT / 'config' / 'config.yaml', 'r') as f:
        return yaml.safe_load(f)


def sync(entid=None, vzjm='%%'):
    """执行连锁目录清单全量同步"""
    config = load_config()
    o = config['oracle']
    entid = entid or 'E4KVKGVOXNV'

    t0 = time.time()
    logger.info(f"开始连锁目录清单同步 | entid={entid} | vzjm={vzjm}")

    # 1. 连 Oracle 执行查询
    oracle = OracleConn(
        host=o['host'], port=o['port'], service_name=o['service_name'],
        username=o['username'], password=resolve_secret(o.get('password', ''), 'ORACLE_PASSWORD'),
        schema=o.get('schema', 'RACE')
    )

    try:
        logger.info("正在 Oracle 执行连锁目录清单查询（可能需要 30~60 秒）...")
        cur = oracle.conn.cursor()
        cur.execute(CHAIN_CATALOG_SQL, entid=entid, vzjm=vzjm)
        cols = [c[0].lower() for c in cur.description]
        rows = cur.fetchall()
        cur.close()
        logger.info(f"Oracle 查询完成: {len(rows):,} 行, {len(cols)} 列")
    finally:
        oracle.close()

    if not rows:
        logger.warning("查询结果为空，跳过写入")
        return 0

    # 2. 写入 DuckDB
    duckdb = DuckDBConn(str(ROOT / config['duckdb']['db_path']))
    try:
        # 建表（CREATE OR REPLACE）
        duckdb.execute(DDL_CHAIN_CATALOG)

        # 批量插入
        import pandas as pd
        df = pd.DataFrame(rows, columns=cols)
        duckdb.conn.register('_chain_df', df)
        duckdb.conn.execute("INSERT INTO raw.chain_catalog SELECT *, CURRENT_TIMESTAMP FROM _chain_df")
        duckdb.conn.unregister('_chain_df')

        # 建表会清掉 COMMENT，重新应用中文备注（幂等）
        try:
            apply_script = ROOT / 'scripts' / 'apply_schema_comments.py'
            if apply_script.exists():
                subprocess.run([sys.executable, str(apply_script), '--apply'],
                               capture_output=True, text=True, timeout=120, cwd=str(ROOT))
                logger.info("中文备注已重新应用")
        except Exception as e:
            logger.warning(f"重新应用备注失败: {e}")

        # 验证
        cnt = duckdb.fetchone("SELECT COUNT(*) FROM raw.chain_catalog")[0]
        logger.info(f"DuckDB 写入完成: raw.chain_catalog = {cnt:,} 行")

        # 基本统计
        stats = duckdb.fetchone("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN COALESCE(place_num,0)=0 AND store_sales_30d > 10 THEN 1 ELSE 0 END) as shortage,
                ROUND(AVG(turnover_days), 1) as avg_turnover,
                SUM(CASE WHEN is_sale='N' OR COALESCE(is_procur,'Y')='N' THEN 1 ELSE 0 END) as stopped
            FROM raw.chain_catalog
        """)
        logger.info(f"统计: 总品项={stats[0]:,}, 缺货(库存=0且30天销量>10)={stats[1]}, "
                    f"平均周转={stats[2]}天, 停售/停采={stats[3]}")

    finally:
        duckdb.close()

    elapsed = time.time() - t0
    logger.info(f"连锁目录清单同步完成, 耗时 {elapsed:.1f}s")
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='连锁目录清单同步')
    parser.add_argument('--entid', default=None, help='企业ID（默认 E4KVKGVOXNV）')
    parser.add_argument('--search', default='%%', help='搜索关键字（默认全量）')
    args = parser.parse_args()

    os.makedirs(ROOT / 'logs', exist_ok=True)
    sync(entid=args.entid, vzjm=args.search)
