"""
nl_query.py -- 自然语言查询接口
OpenClaw 调用此模块将自然语言翻译成 SQL 并执行

用法：
  python3 -m src.utils.nl_query "今天卖了多少"
  python3 -m src.utils.nl_query "本周TOP10畅销商品"
  python3 -m src.utils.nl_query "哪些商品库存不足"
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import DuckDBConn
import yaml

# 安全规则
BLOCKED_KEYWORDS = [
    'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE',
    'TRUNCATE', 'GRANT', 'REVOKE', 'ATTACH', 'DETACH', 'PRAGMA',
    'EXPORT', 'COPY', 'IMPORT'
]
MAX_ROWS = 1000
QUERY_TIMEOUT_SECONDS = 10
SENSITIVE_COLUMNS = ['MOBILE', 'TELEPHONE', 'IDCARD', 'EMAIL', 'FAMILYPHONE', 'PASSWORD']


def load_config():
    with open(ROOT / 'config' / 'config.yaml') as f:
        return yaml.safe_load(f)


def validate_sql(sql: str) -> tuple:
    """SQL 安全校验，返回 (is_safe, reason)"""
    sql_upper = sql.upper().strip()

    # 必须是 SELECT
    if not sql_upper.startswith('SELECT') and not sql_upper.startswith('WITH'):
        return False, "只允许 SELECT 查询"

    # 检查危险关键词
    for kw in BLOCKED_KEYWORDS:
        # 用单词边界匹配，避免误杀（如 SELECT * FROM table_name 不应匹配 DELETE）
        if re.search(rf'\b{kw}\b', sql_upper):
            return False, f"SQL 包含禁止的关键词: {kw}"

    # 自动加 LIMIT
    if 'LIMIT' not in sql_upper:
        sql = sql.rstrip(';').rstrip() + f' LIMIT {MAX_ROWS}'

    return True, sql


def mask_sensitive(rows: list, col_names: list) -> list:
    """脱敏敏感字段"""
    sensitive_idx = [i for i, c in enumerate(col_names) if c.upper() in SENSITIVE_COLUMNS]
    if not sensitive_idx:
        return rows
    for row in rows:
        for idx in sensitive_idx:
            if row[idx]:
                row = list(row)
                row[idx] = '***'
    return rows


def execute_query(sql: str) -> dict:
    """执行 SQL 查询，返回结构化结果"""
    # 安全校验
    is_safe, result = validate_sql(sql)
    if not is_safe:
        return {'success': False, 'error': result, 'sql': sql}

    safe_sql = result  # 校验通过后 result 是处理过的 SQL

    # 执行（只读模式，不抢写锁）
    db = DuckDBConn(str(ROOT / load_config()['duckdb']['db_path']), read_only=True)
    start = time.time()
    try:
        cur = db.conn.execute(safe_sql)
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
        elapsed = time.time() - start

        # 脱敏
        rows = mask_sensitive(rows, col_names)

        return {
            'success': True,
            'sql': safe_sql,
            'columns': col_names,
            'rows': rows,
            'row_count': len(rows),
            'elapsed': round(elapsed, 3)
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            'success': False,
            'error': str(e),
            'sql': safe_sql,
            'elapsed': round(elapsed, 3)
        }
    finally:
        db.close()


def format_result(result: dict) -> str:
    """格式化查询结果为可读文本"""
    if not result['success']:
        return f"❌ 查询失败: {result['error']}"

    cols = result['columns']
    rows = result['rows']
    n = result['row_count']
    elapsed = result['elapsed']

    if n == 0:
        return "查询成功，但没有数据。"

    # 计算列宽
    col_widths = []
    for i, c in enumerate(cols):
        max_val_len = max([len(str(r[i])) for r in rows[:20]]) if rows else 0
        col_widths.append(min(max(len(c), max_val_len) + 2, 30))

    # 表头
    header = "".join([str(c).ljust(col_widths[i]) for i, c in enumerate(cols)])
    separator = "-" * len(header)

    # 数据行
    lines = [header, separator]
    for row in rows[:50]:
        line = "".join([str(row[i] or '').ljust(col_widths[i]) for i in range(len(cols))])
        lines.append(line)

    if n > 50:
        lines.append(f"... 共 {n} 行，仅显示前 50 行")

    lines.append(f"\n⏱ {elapsed}s | {n} 行")
    return "\n".join(lines)


# ============================================
# 查询模板 -- 高频问题快速命中
# ============================================
TEMPLATES = [
    {
        'keywords': ['退款', '退货', '退单'],
        'sql': """
            SELECT COUNT(*) as 退款单数, COALESCE(SUM(REFUNDFEE), 0) as 退款金额
            FROM raw.K_D3OMS_ORDERREFUNDMT
            WHERE TRY_CAST(LASTMODIFYTIME AS DATE) = CURRENT_DATE
        """,
        'answer': lambda r: f"今日退款 {r[0][0]} 单，退款金额 ¥{r[0][1]:,.2f}"
    },
    {
        'keywords': ['低库存', '缺货', '库存不足', '库存预警'],
        'sql': """
            SELECT p.product_name, p.specification, a.PLACENUM as 当前库存, a.STORMIN as 预警阈值
            FROM raw.ANGLEBALANCE a
            JOIN dim.product p ON a.GOODSID = p.product_id
            WHERE a.PLACENUM < a.STORMIN AND a.STORMIN > 0
            ORDER BY a.PLACENUM ASC
            LIMIT 20
        """,
        'answer': lambda r: f"低库存商品 {len(r)} 个" if r else "当前无低库存商品"
    },
    {
        'keywords': ['过期', '效期', '近效期'],
        'sql': """
            SELECT p.product_name, b.BATCHCODE as 批号, b.VALDATE as 有效期,
                   p.specification
            FROM raw.BATCHCODE b
            JOIN dim.product p ON b.GOODSID = p.product_id
            WHERE TRY_CAST(b.VALDATE AS DATE) <= CURRENT_DATE + 90
              AND TRY_CAST(b.VALDATE AS DATE) >= CURRENT_DATE
            ORDER BY b.VALDATE ASC
            LIMIT 20
        """,
        'answer': lambda r: f"90天内近效期商品 {len(r)} 个" if r else "无近效期商品"
    },
    {
        'keywords': ['top', '畅销', '排行', '销量排名', '热销'],
        'sql': """
            SELECT p.product_name, p.specification,
                   SUM(d.NUM) as 数量, SUM(d.AMOUNT) as 金额
            FROM raw.SALEOUTDT d
            JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
            JOIN dim.product p ON d.GOODSID = p.product_id
            WHERE TRY_CAST(m.DATES AS DATE) >= CURRENT_DATE - 6
            GROUP BY p.product_name, p.specification
            ORDER BY 金额 DESC
            LIMIT 20
        """,
        'answer': lambda r: f"近7天畅销商品 TOP {len(r)}"
    },
    {
        'keywords': ['趋势', '近7天', '七天', '7天', '每天'],
        'sql': """
            SELECT TRY_CAST(m.DATES AS DATE) as 日期,
                   COUNT(*) as 单数,
                   SUM(m.TAXAMOUNT) as 含税销售额,
                   SUM(m.PROFIT) as 毛利
            FROM raw.SALEOUTMT m
            WHERE TRY_CAST(m.DATES AS DATE) >= CURRENT_DATE - 6
            GROUP BY 1
            ORDER BY 1
        """,
        'answer': lambda r: f"近7天销售趋势：{len(r)} 天有数据"
    },
    {
        'keywords': ['仓库发货', '各仓库', '仓库销售'],
        'sql': """
            SELECT w.warehouse_name, COUNT(m.BILLNO) as 单数,
                   COALESCE(SUM(d.AMOUNT), 0) as 金额
            FROM raw.SALEOUTMT m
            JOIN raw.SALEOUTDT d ON m.BILLNO = d.BILLNO
            JOIN dim.warehouse w ON d.WHID = w.warehouse_id
            WHERE TRY_CAST(m.DATES AS DATE) = CURRENT_DATE
            GROUP BY w.warehouse_name
            ORDER BY 金额 DESC
        """,
        'answer': lambda r: f"今日各仓库发货：{len(r)} 个仓库有数据"
    },
    {
        'keywords': ['商品数', '多少商品', '商品总数'],
        'sql': "SELECT COUNT(*) FROM dim.product WHERE status = 'ACTIVE'",
        'answer': lambda r: f"活跃商品 {r[0][0]} 个"
    },
    {
        'keywords': ['客户数', '多少客户', '客户总数'],
        'sql': "SELECT COUNT(*) FROM dim.customer WHERE status = 'ACTIVE'",
        'answer': lambda r: f"活跃客户 {r[0][0]} 个"
    },
    {
        'keywords': ['昨天', '昨日'],
        'sql': """
            SELECT COUNT(*) as 单数,
                   COALESCE(SUM(TAXAMOUNT), 0) as 含税销售额,
                   COALESCE(SUM(PROFIT), 0) as 毛利
            FROM raw.SALEOUTMT
            WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE - 1
        """,
        'answer': lambda r: f"昨日销售：{r[0][0]} 单，含税销售额 ¥{r[0][1]:,.2f}，毛利 ¥{r[0][2]:,.2f}"
    },
    {
        'keywords': ['今天', '今日', '卖了', '销售额', 'kpi'],
        'sql': """
            SELECT COUNT(*) as 单数,
                   COALESCE(SUM(TAXAMOUNT), 0) as 含税销售额,
                   COALESCE(SUM(AMOUNT), 0) as 不含税销售额,
                   COALESCE(SUM(PROFIT), 0) as 毛利,
                   COALESCE(SUM(COSTAMT), 0) as 成本
            FROM raw.SALEOUTMT
            WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE
        """,
        'answer': lambda r: f"今日销售：{r[0][0]} 单，含税销售额 ¥{r[0][1]:,.2f}，毛利 ¥{r[0][3]:,.2f}" + (f"，毛利率 {r[0][3]/r[0][1]*100:.1f}%" if r[0][1] else "")
    },
]


def try_template(question: str) -> dict:
    """尝试匹配查询模板，命中则直接执行"""
    question_lower = question.lower()

    for tpl in TEMPLATES:
        if any(kw in question_lower for kw in tpl['keywords']):
            result = execute_query(tpl['sql'])
            if result['success'] and result['rows']:
                answer = tpl['answer'](result['rows'])
                return {
                    'matched': True,
                    'answer': answer,
                    'detail': format_result(result),
                    'sql': result['sql']
                }
            elif result['success'] and not result['rows']:
                return {
                    'matched': True,
                    'answer': '查询成功，暂无数据。',
                    'detail': '',
                    'sql': result['sql']
                }
    return {'matched': False}


def main():
    if len(sys.argv) < 2:
        print("用法: python3 -m src.utils.nl_query '你的问题'")
        print("示例:")
        print("  python3 -m src.utils.nl_query '今天卖了多少'")
        print("  python3 -m src.utils.nl_query '库存不足的商品'")
        print("  python3 -m src.utils.nl_query '近7天销售趋势'")
        return

    question = ' '.join(sys.argv[1:])

    # 1. 尝试模板匹配
    result = try_template(question)
    if result['matched']:
        print(result['answer'])
        if result['detail']:
            print()
            print(result['detail'])
        return

    # 2. 未命中模板 -- 提示用户
    print(f"未匹配到查询模板。")
    print(f"你可以直接用 SQL 查询：")
    print(f"  python3 -m src.utils.query sql \"你的SQL\"")
    print(f"\n支持的查询关键词：")
    for tpl in TEMPLATES:
        print(f"  {tpl['keywords']}")


if __name__ == '__main__':
    main()
