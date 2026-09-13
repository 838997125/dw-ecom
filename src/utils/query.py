"""
query.py - DuckDB 查询工具
提供日常查询和 KPI 聚合功能

用法：
  python3 -m src.utils.query kpi              # 今日 KPI 汇总
  python3 -m src.utils.query daily --date 2026-07-22  # 指定日期销售汇总
  python3 -m src.utils.query sql "SELECT COUNT(*) FROM raw.SALEOUTMT"  # 自定义SQL
  python3 -m src.utils.query tables           # 列出所有表及行数
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import DuckDBConn


def get_conn(read_only: bool = True) -> DuckDBConn:
    import yaml
    with open(ROOT / 'config' / 'config.yaml') as f:
        config = yaml.safe_load(f)
    # 默认 read_only=True，避免查询路径和 CDC 写锁互斥
    return DuckDBConn(str(ROOT / config['duckdb']['db_path']), read_only=read_only)


def show_tables(db: DuckDBConn):
    """列出所有表及行数"""
    tables = db.fetchall("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('raw', 'agg', 'dim')
        ORDER BY table_schema, table_name
    """)
    print(f"\n{'Schema':<8} {'表名':<30} {'行数':>10}")
    print("-" * 52)
    for schema, name in tables:
        try:
            cnt = db.fetchone(f'SELECT COUNT(*) FROM {schema}."{name}"')[0]
        except:
            cnt = '-'
        print(f"{schema:<8} {name:<30} {cnt:>10}")


def show_kpi(db: DuckDBConn):
    """今日 KPI 汇总"""
    print("\n📊 今日 KPI 汇总")
    print("=" * 60)

    # 今日销售出库
    row = db.fetchone("""
        SELECT COUNT(*) as orders, COALESCE(SUM(TAXAMOUNT), 0) as amount,
               COALESCE(SUM(PROFIT), 0) as profit, COALESCE(SUM(COSTAMT), 0) as cost
        FROM raw.SALEOUTMT
        WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE
    """)
    if row:
        print(f"  销售出库单: {row[0]} 单")
        print(f"  销售额(含税): ¥{row[1]:,.2f}")
        print(f"  销售成本: ¥{row[3]:,.2f}")
        print(f"  毛利: ¥{row[2]:,.2f}")
        if row[1] and row[1] > 0:
            print(f"  毛利率: {row[2]/row[1]*100:.1f}%")

    # 今日订单明细数
    row = db.fetchone("""
        SELECT COUNT(*), COALESCE(SUM(NUM), 0), COALESCE(SUM(AMOUNT), 0)
        FROM raw.SALEOUTDT d
        JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
        WHERE TRY_CAST(m.DATES AS DATE) = CURRENT_DATE
    """)
    if row:
        print(f"  出库明细: {row[0]} 条, 数量: {row[1]:,.0f}")

    # 今日退款
    row = db.fetchone("""
        SELECT COUNT(*), COALESCE(SUM(REFUNDFEE), 0)
        FROM raw.K_D3OMS_ORDERREFUNDMT
        WHERE TRY_CAST(LASTMODIFYTIME AS DATE) = CURRENT_DATE
    """)
    if row:
        print(f"  退款单: {row[0]} 单, 退款金额: ¥{row[1]:,.2f}")

    # 商品/客户总数
    goods_cnt = db.fetchone("SELECT COUNT(*) FROM raw.GOODSDOC")[0]
    client_cnt = db.fetchone("SELECT COUNT(*) FROM raw.CLIENTDOC")[0]
    print(f"\n  商品档案: {goods_cnt} 条")
    print(f"  客户档案: {client_cnt} 条")
    print("=" * 60)


def show_daily(db: DuckDBConn, date_str: str = None):
    """指定日期销售汇总"""
    where = f"TRY_CAST(DATES AS DATE) = '{date_str}'" if date_str else "TRY_CAST(DATES AS DATE) = CURRENT_DATE"

    print(f"\n📅 销售汇总 ({date_str or '今天'})")
    print("=" * 60)

    # 按仓库汇总
    rows = db.fetchall(f"""
        SELECT s.WHNAME, COUNT(m.BILLNO), COALESCE(SUM(d.AMOUNT), 0), COALESCE(SUM(d.NUM), 0)
        FROM raw.SALEOUTMT m
        JOIN raw.SALEOUTDT d ON m.BILLNO = d.BILLNO
        LEFT JOIN raw.STOREHOUSE s ON d.WHID = s.WHID
        WHERE {where}
        GROUP BY s.WHNAME
        ORDER BY COALESCE(SUM(d.AMOUNT), 0) DESC
    """)
    if rows:
        print(f"  {'仓库':<20} {'单数':>6} {'金额':>14} {'数量':>10}")
        print("  " + "-" * 54)
        for r in rows:
            print(f"  {(r[0] or '未知'):<20} {r[1]:>6} ¥{r[2]:>12,.2f} {r[3]:>10,.0f}")

    # 按客户类型汇总
    rows = db.fetchall(f"""
        SELECT c.CLIENTTYPE, COUNT(DISTINCT m.CLIENTID), COUNT(m.BILLNO), COALESCE(SUM(d.AMOUNT), 0)
        FROM raw.SALEOUTMT m
        JOIN raw.SALEOUTDT d ON m.BILLNO = d.BILLNO
        LEFT JOIN raw.CLIENTDOC c ON m.CLIENTID = c.CLIENTID
        WHERE {where}
        GROUP BY c.CLIENTTYPE
        ORDER BY COALESCE(SUM(d.AMOUNT), 0) DESC
    """)
    if rows:
        print(f"\n  按客户类型:")
        print(f"  {'类型':<12} {'客户数':>6} {'单数':>6} {'金额':>14}")
        print("  " + "-" * 42)
        for r in rows:
            print(f"  {(r[0] or '未知'):<12} {r[1]:>6} {r[2]:>6} ¥{r[3]:>12,.2f}")

    print("=" * 60)


def run_sql(db: DuckDBConn, sql: str):
    """执行自定义 SQL"""
    try:
        result = db.conn.execute(sql)
        # 尝试 fetchall（如果是 SELECT）
        try:
            rows = result.fetchall()
            if rows:
                cols = [d[0] for d in result.description]
                print(" | ".join(cols))
                print("-" * 80)
                for row in rows[:50]:  # 最多显示50行
                    print(" | ".join([str(v) for v in row]))
                if len(rows) > 50:
                    print(f"... 共 {len(rows)} 行，仅显示前 50 行")
            else:
                print("(无结果)")
        except:
            print(f"执行完成 (affected rows: {result.fetchall() if hasattr(result, 'fetchall') else '?'})")
    except Exception as e:
        print(f"SQL 错误: {e}")


def main():
    parser = argparse.ArgumentParser(description='DuckDB 查询工具')
    parser.add_argument('command', choices=['kpi', 'daily', 'sql', 'tables'],
                        help='kpi=今日KPI, daily=日销售汇总, sql=自定义SQL, tables=表列表')
    parser.add_argument('--date', '-d', help='日期 (YYYY-MM-DD)')
    parser.add_argument('sql', nargs='?', help='SQL 语句 (sql 命令用)')

    args = parser.parse_args()
    db = get_conn()

    try:
        if args.command == 'tables':
            show_tables(db)
        elif args.command == 'kpi':
            show_kpi(db)
        elif args.command == 'daily':
            show_daily(db, args.date)
        elif args.command == 'sql':
            if not args.sql:
                print("请提供 SQL 语句")
                return
            run_sql(db, args.sql)
    finally:
        db.close()


if __name__ == '__main__':
    main()
