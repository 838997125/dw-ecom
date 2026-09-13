#!/usr/bin/env python3
"""近效期商品报告生成脚本

用法:
  python3 near_expiry_report.py                    # 默认90天，发给<USER_NAME>
  python3 near_expiry_report.py --days 180          # 查180天
  python3 near_expiry_report.py --to 代少波          # 发给代少波
  python3 near_expiry_report.py --no-send           # 只生成不发送
"""
import argparse
import os
import subprocess
import sys
import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / 'data' / 'duckdb' / 'ecom.duckdb'
OUTPUT_DIR = ROOT / 'outputs'
DWS = Path.home() / '.local' / 'bin' / 'dws'

DEFAULT_RECIPIENT = '<USER_NAME>'
DEFAULT_OPEN_DINGTALK_ID = '<DINGTALK_OPEN_CONVERSATION_ID>'


def query_data(db_path, days=90):
    conn = duckdb.connect(db_path)
    df = conn.execute(f"""
        WITH latest_price AS (
            SELECT d.GOODSID, d.BATCHCODE,
                   d.PRICE as 单价, d.TAXPRICE as 含税单价, d.COSTAMT as 单位成本
            FROM raw.SALEOUTDT d
            JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
            WHERE d.PRICE > 0
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY d.GOODSID, d.BATCHCODE
                ORDER BY TRY_CAST(m.DATES AS DATE) DESC
            ) = 1
        )
        SELECT
            p.product_name as 商品名,
            p.specification as 规格,
            p.manufacturer as 厂家,
            p.barcode as 条形码,
            b.BATCHCODE as 批号,
            b.PRODUCEDATE as 生产日期,
            b.VALDATE as 有效期,
            a.PLACENUM as 库存量,
            ROUND(lp.单价, 2) as 单价,
            ROUND(lp.含税单价, 2) as 含税单价,
            ROUND(lp.单位成本, 2) as 单位成本,
            ROUND(a.PLACENUM * COALESCE(lp.含税单价, 0), 2) as 库存货值,
            DATE_DIFF('day', CURRENT_DATE, TRY_CAST(b.VALDATE AS DATE)) as 剩余天数,
            CASE
                WHEN TRY_CAST(b.VALDATE AS DATE) < CURRENT_DATE THEN '已过期'
                WHEN TRY_CAST(b.VALDATE AS DATE) <= CURRENT_DATE + 30 THEN '30天内到期'
                WHEN TRY_CAST(b.VALDATE AS DATE) <= CURRENT_DATE + 60 THEN '60天内到期'
                WHEN TRY_CAST(b.VALDATE AS DATE) <= CURRENT_DATE + 90 THEN '90天内到期'
            END as 状态
        FROM raw.BATCHCODE b
        JOIN dim.product p ON b.GOODSID = p.product_id
        JOIN raw.ANGLEBALANCE a ON b.ANGLEID = a.ANGLEID
        LEFT JOIN latest_price lp ON b.GOODSID = lp.GOODSID AND b.BATCHCODE = lp.BATCHCODE
        WHERE TRY_CAST(b.VALDATE AS DATE) <= CURRENT_DATE + {days}
          AND TRY_CAST(b.VALDATE AS DATE) >= CURRENT_DATE - 180
          AND a.PLACENUM > 0
        ORDER BY b.VALDATE ASC
    """).fetchdf()
    conn.close()

    dedup_cols = ['商品名', '规格', '厂家', '条形码', '批号', '生产日期', '有效期', '库存量']
    df = df.drop_duplicates(subset=dedup_cols, keep='first').reset_index(drop=True)
    return df


def generate_excel(df, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(str(output_path), engine='openpyxl') as writer:
        summary = df.groupby('状态').agg(
            批次数=('批号', 'count'),
            涉及商品数=('商品名', 'nunique'),
            总库存量=('库存量', 'sum'),
            库存货值=('库存货值', 'sum')
        ).reset_index()
        summary.to_excel(writer, sheet_name='汇总', index=False)
        for status in ['已过期', '30天内到期', '60天内到期', '90天内到期']:
            sub = df[df['状态'] == status].copy()
            sub.to_excel(writer, sheet_name=status, index=False)
        df.to_excel(writer, sheet_name='全部明细', index=False)


def resolve_recipient(name):
    if name == DEFAULT_RECIPIENT:
        return DEFAULT_OPEN_DINGTALK_ID
    cmd = [str(DWS), 'contact', 'user', 'search', '--query', name, '--format', 'json']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        data = json.loads(result.stdout)
        if data.get('result'):
            return data['result'][0]['openDingTalkId']
    return None


def send_dingtalk(file_path, open_dingtalk_id):
    cmd = [
        str(DWS), 'chat', 'message', 'send',
        '--open-dingtalk-id', open_dingtalk_id,
        '--msg-type', 'file',
        '--file-path', file_path,
        '--title', '近效期商品明细',
        '--format', 'json', '-y'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode == 0, result.stdout


def main():
    parser = argparse.ArgumentParser(description='近效期商品报告')
    parser.add_argument('--days', type=int, default=90, help='查询天数范围 (默认90)')
    parser.add_argument('--to', type=str, default=DEFAULT_RECIPIENT, help='收件人姓名 (默认<USER_NAME>)')
    parser.add_argument('--no-send', action='store_true', help='只生成不发送')
    args = parser.parse_args()

    print(f'查询近效期商品 (范围: {args.days}天)...')
    df = query_data(str(DB_PATH), args.days)
    print(f'查到 {len(df)} 条 (已去重，库存>0)')

    if len(df) == 0:
        print('无近效期商品数据')
        return

    today = datetime.now().strftime('%Y-%m-%d')
    today_compact = datetime.now().strftime('%Y%m%d')
    output_path = OUTPUT_DIR / today / f'近效期商品明细_{today_compact}.xlsx'
    generate_excel(df, output_path)
    print(f'Excel: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)')

    for status in ['已过期', '30天内到期', '60天内到期', '90天内到期']:
        sub = df[df['状态'] == status]
        if len(sub) > 0:
            print(f'  {status}: {len(sub)} 条, 库存 {sub["库存量"].sum():.0f}, 货值 ¥{sub["库存货值"].sum():,.0f}')

    if not args.no_send:
        open_id = resolve_recipient(args.to)
        if open_id:
            success, resp = send_dingtalk(str(output_path), open_id)
            if success:
                print(f'✅ 已发送给 {args.to}')
            else:
                print(f'❌ 发送失败: {resp}')
        else:
            print(f'❌ 找不到钉钉用户: {args.to}')


if __name__ == '__main__':
    main()
