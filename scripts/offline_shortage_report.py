#!/usr/bin/env python3
"""
offline_shortage_report.py - 线下缺货率日报

每日 8:00 由 OpenClaw cron 触发，计算前一天的线下缺货率：
  分子 = chain_catalog 中 线下门店30天销量>10 且 合格库可销库存=0
         且 冻结=N 且 停采=N 且 停售=N 的记录数
  分母 = chain_catalog 中 停采=N 且 停售=N 的记录数
  线下缺货率 = 分子 / 分母

同时写入钉钉 AI 表格「连锁门店日报数据 / 线下缺货率」：
  baseId = <DINGTALK_BASE_ID>
  tableId = hERWDMS

用法：
  python3 scripts/offline_shortage_report.py            # 正常执行（报错日期=昨天）
  python3 scripts/offline_shortage_report.py --dry-run  # 只算不写钉钉
"""
import sys
import os
import time
import json
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
import logging

os.makedirs(ROOT / 'logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / 'logs' / 'shortage_report.log', encoding='utf-8')
    ]
)
logger = logging.getLogger('shortage_report')

DB_PATH = str(ROOT / 'data' / 'duckdb' / 'ecom.duckdb')


def connect_ro(retries: int = 60, delay: float = 2.0):
    """只读连接 DuckDB，遇到 CDC 写锁时重试。

    DuckDB 同一时刻只允许一个写连接；CDC daemon 每轮增量会短暂持有写锁，
    只读连接在写锁持有期间也会报 Conflicting lock。这里轮询等待锁释放，
    默认最多等 120s（60 次 x 2s），覆盖 CDC 单轮增量窗口。
    """
    last_err = None
    for i in range(retries):
        try:
            return duckdb.connect(DB_PATH, read_only=True)
        except duckdb.IOException as e:
            last_err = e
            if 'Conflicting lock' not in str(e) and 'lock' not in str(e).lower():
                raise
            if i == 0:
                logger.warning(f"DuckDB 被占用（CDC 写窗口），等待锁释放...")
            time.sleep(delay)
    raise last_err

BASE_ID = '<DINGTALK_BASE_ID>'
TABLE_ID = 'hERWDMS'

# 钉钉表字段映射（2026-08-17 创建）
FIELDS = {
    'date':          '01ZM8y7',  # 日期
    'total':         '3gCZF9e',  # 总条目数
    'shortage':      'xwLLMw0',  # 缺货条目数
    'rate':          'jsOsorm',  # 线下缺货率
    'delta':         '8AkfUst',  # 较昨日变化
    'severe':        'vuBaQvY',  # 严重缺货数
    'has_transit':   '09PgEtN',  # 缺货有在途数
    'daily_loss':    'UOfAWH7',  # 日销损失预估
    'qh_7d':         'YiPAP3N',  # 近7天请货缺口
    'stock_value':   '5Fz3G2N',  # 库存总金额
    'delivery_avg':  '1LZSrRl',  # 日均配送额
    'turnover':      'VnQmo9j',  # 库存周转天数
    'note':          'gQjMcBa',  # 数据说明
}

# 缺货口径（用户指定 2026-08-17）：
SHORTAGE_WHERE = """
store_sales_30d > 10
AND COALESCE(place_num, 0) = 0
AND COALESCE(is_freeze, 'N') = 'N'
AND COALESCE(is_procur, 'N') = 'N'
AND COALESCE(is_sale, 'N') = 'N'
"""
DENOM_WHERE = """
COALESCE(is_procur, 'N') = 'N'
AND COALESCE(is_sale, 'N') = 'N'
"""


def compute(report_date: str):
    """从 DuckDB chain_catalog 计算缺货率及补充指标"""
    con = connect_ro()
    try:
        r = con.execute(f"""
        SELECT
            SUM(CASE WHEN {SHORTAGE_WHERE} THEN 1 ELSE 0 END),
            SUM(CASE WHEN {DENOM_WHERE} THEN 1 ELSE 0 END)
        FROM raw.chain_catalog
        """).fetchone()
        shortage, total = r[0] or 0, r[1] or 0
        rate = shortage / total if total else 0

        # 严重缺货（30天销量>100）
        severe = con.execute(f"""
        SELECT COUNT(*) FROM raw.chain_catalog
        WHERE store_sales_30d > 100 AND {SHORTAGE_WHERE}
        """).fetchone()[0]

        # 缺货但有在途
        has_transit = con.execute(f"""
        SELECT COUNT(*) FROM raw.chain_catalog
        WHERE {SHORTAGE_WHERE} AND COALESCE(in_transit_qty, 0) > 0
        """).fetchone()[0]

        # 缺货预估日销损失 = 缺货品的日均销量 x 进价
        daily_loss = con.execute(f"""
        SELECT ROUND(SUM(store_sales_30d / 30.0 * COALESCE(tax_price, 0)), 2)
        FROM raw.chain_catalog WHERE {SHORTAGE_WHERE}
        """).fetchone()[0] or 0

        # 近7天请货缺口合计
        qh_7d = con.execute(f"""
        SELECT ROUND(SUM(COALESCE(shortage_7d, 0)), 0)
        FROM raw.chain_catalog WHERE {SHORTAGE_WHERE}
        """).fetchone()[0] or 0

        # 库存周转口径（分子库存金额 / 日均配送额）
        v = con.execute(f"""
        SELECT
            SUM(place_num * COALESCE(min_purchase_price, tax_price, 0)),
            SUM(delivery_30d * COALESCE(min_purchase_price, tax_price, 0))
        FROM raw.chain_catalog WHERE {DENOM_WHERE}
        """).fetchone()
        stock_value = round(v[0] or 0, 2)
        delivery_avg = round((v[1] or 0) / 30.0, 2)
        turnover_days = round(stock_value / delivery_avg, 1) if delivery_avg else None

        return {
            'report_date': report_date,
            'total': total,
            'shortage': shortage,
            'rate': round(rate, 6),
            'rate_pct': round(rate * 100, 2),
            'severe': severe,
            'has_transit': has_transit,
            'daily_loss': daily_loss,
            'qh_7d': qh_7d,
            'stock_value': stock_value,
            'delivery_avg': delivery_avg,
            'turnover_days': turnover_days,
        }
    finally:
        con.close()


def query_last_rate(report_date: str):
    """查钉钉表中最近一条(非当日)记录的缺货率（算较昨日变化用）"""
    try:
        out = subprocess.run(
            ['dws', 'aitable', 'record', 'query',
             '--base-id', BASE_ID, '--table-id', TABLE_ID,
             '--limit', '100', '--format', 'json'],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            logger.warning(f"查询钉钉历史失败: {out.stderr[:200]}")
            return None
        data = json.loads(out.stdout)
        records = data.get('data', {}).get('records', [])
        rate_fld, date_fld = FIELDS['rate'], FIELDS['date']
        # 收集 (日期, 缺货率)，排除当日，取日期最大的一条
        candidates = []
        for rec in records:
            cells = rec.get('cells', {})
            dv, rv = cells.get(date_fld), cells.get(rate_fld)
            if not dv or rv is None:
                continue
            ds = str(dv)[:10]
            if ds >= report_date:  # 只和历史比，排除当日及未来
                continue
            try:
                candidates.append((ds, float(rv)))
            except (ValueError, TypeError):
                continue
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]
    except Exception as e:
        logger.warning(f"查询钉钉历史异常: {e}")
        return None


def write_to_dingtalk(metrics: dict, delta_pp: float = None):
    """写入钉钉 AI 表格（先查重，同日期已存在则更新）"""
    date_fld = FIELDS['date']

    # 1. 查同日期记录是否已存在（幂等：重跑不重复插）
    out = subprocess.run(
        ['dws', 'aitable', 'record', 'query',
         '--base-id', BASE_ID, '--table-id', TABLE_ID,
         '--limit', '100', '--format', 'json'],
        capture_output=True, text=True, timeout=60)
    existing_rec_id = None
    if out.returncode == 0:
        try:
            records = json.loads(out.stdout).get('data', {}).get('records', [])
            for rec in records:
                v = rec.get('cells', {}).get(date_fld)
                if v and str(v)[:10] == metrics['report_date']:
                    existing_rec_id = rec.get('recordId')
                    break
        except Exception as e:
            logger.warning(f"解析已有记录失败: {e}")

    cells = {
        FIELDS['date']:         metrics['report_date'],
        FIELDS['total']:        metrics['total'],
        FIELDS['shortage']:     metrics['shortage'],
        FIELDS['rate']:         metrics['rate'],
        FIELDS['severe']:       metrics['severe'],
        FIELDS['has_transit']:  metrics['has_transit'],
        FIELDS['daily_loss']:   metrics['daily_loss'],
        FIELDS['qh_7d']:        metrics['qh_7d'],
        FIELDS['stock_value']:  metrics['stock_value'],
        FIELDS['delivery_avg']: metrics['delivery_avg'],
    }
    if metrics['turnover_days'] is not None:
        cells[FIELDS['turnover']] = metrics['turnover_days']
    if delta_pp is not None:
        cells[FIELDS['delta']] = delta_pp

    note = (f"缺货口径: 30天销量>10且合格库库存=0且非冻结/停采/停售; "
            f"数据源: 时空ERP连锁目录清单(每日7:10刷新)")
    cells[FIELDS['note']] = note

    records_json = json.dumps([{'recordId': existing_rec_id, 'cells': cells}]) if existing_rec_id \
        else json.dumps([{'cells': cells}])

    cmd = (['dws', 'aitable', 'record', 'update',
             '--base-id', BASE_ID, '--table-id', TABLE_ID,
             '--records', records_json, '--format', 'json']
           if existing_rec_id else
           ['dws', 'aitable', 'record', 'create',
            '--base-id', BASE_ID, '--table-id', TABLE_ID,
            '--records', records_json, '--format', 'json'])

    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"钉钉写入失败: {out.stderr[:300]}")
    action = 'updated' if existing_rec_id else 'created'
    logger.info(f"钉钉记录已{action}: {metrics['report_date']}")
    return existing_rec_id or json.loads(out.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    report_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    t0 = time.time()
    logger.info(f"=== 线下缺货率日报 {report_date} ===")

    # 1. 检查 chain_catalog 数据新鲜度
    con = connect_ro()
    try:
        etl = con.execute("SELECT MAX(_etl_time) FROM raw.chain_catalog").fetchone()[0]
        if etl is None:
            raise RuntimeError("raw.chain_catalog 为空，请先运行 sync_chain_catalog.py")
        age_h = (datetime.now() - etl).total_seconds() / 3600
        if age_h > 30:
            logger.warning(f"chain_catalog 数据已 {age_h:.0f} 小时未刷新，结果可能过期")
    finally:
        con.close()

    # 2. 计算
    m = compute(report_date)
    logger.info(f"总条目={m['total']}, 缺货={m['shortage']}, "
                f"缺货率={m['rate_pct']}%")

    # 3. 较昨日变化（百分点）
    last_rate = query_last_rate(report_date)
    delta_pp = None
    if last_rate is not None:
        delta_pp = round((m['rate'] - last_rate) * 100, 2)
        logger.info(f"昨日缺货率={last_rate*100:.2f}%, 变化={delta_pp:+.2f}pp")

    # 4. 写钉钉
    if args.dry_run:
        logger.info("[dry-run] 跳过钉钉写入")
    else:
        write_to_dingtalk(m, delta_pp)

    elapsed = time.time() - t0
    logger.info(f"=== 完成, 耗时 {elapsed:.1f}s ===")

    # stdout 摘要（cron payload 可引用）
    print(json.dumps({
        'date': m['report_date'],
        'total': m['total'],
        'shortage': m['shortage'],
        'rate_pct': m['rate_pct'],
        'delta_pp': delta_pp,
        'severe': m['severe'],
        'has_transit': m['has_transit'],
        'daily_loss': m['daily_loss'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
