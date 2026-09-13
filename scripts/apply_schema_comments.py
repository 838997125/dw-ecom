# -*- coding: utf-8 -*-
"""
apply_schema_comments.py - 将中文备注写入 DuckDB (COMMENT ON TABLE/COLUMN)

幂等脚本：可重复运行。优先用词典中的人工精确翻译，否则用通用词典自动推断。
用法:
    python3 scripts/apply_schema_comments.py            # 只输出将要写入的备注
    python3 scripts/apply_schema_comments.py --apply    # 实际写入 DuckDB
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
import yaml
import json

from scripts.schema_comment_dict import (
    TABLE_COMMENTS, MANUAL_OVERRIDE, translate_field,
)

AUTH_JSON = ROOT / "scripts" / "erp_authoritative.json"


def load_authoritative():
    """加载 DBA 提供的权威字段字典（最高优先级）。
    返回 (table_meta, field_map): field_map[TABLE_UPPER][COL_UPPER] = 中文含义"""
    if not AUTH_JSON.exists():
        return {}, {}
    data = json.loads(AUTH_JSON.read_text(encoding="utf-8"))
    return data.get("tables", {}), data.get("fields", {})

DB_PATH = ROOT / "config" / "config.yaml"

# DBA 文档勘误（经实际数据验证，文档写反的字段）。优先级最高。
# 证据：MAXSALEP 在 89% 的商品中 > 实际售价 SALEP（均值26.25 vs 23.87），
#   符合“最高售价/挂牌价上限”，与文档“最低售价”矛盾；同库 STORMAX=最大库存 也印证 Max=大。
AUTH_CORRECTIONS = {
    "GOODSATTR": {
        "MAXSALEP": "最高售价",
        "MINSALEP": "最低售价",
        "MAXRETAIP": "最高零售价",
        "MINRETAIP": "最低零售价",
    },
}


def load_db_path():
    with open(DB_PATH) as f:
        cfg = yaml.safe_load(f)
    return ROOT / cfg["duckdb"]["db_path"]


def main():
    apply = "--apply" in sys.argv
    db = load_db_path()

    con = duckdb.connect(str(db), read_only=not apply)

    auth_tables, auth_fields = load_authoritative()

    # 收集所有表
    tables = con.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('raw', 'dim', 'agg')
        ORDER BY table_schema, table_name
    """).fetchall()

    plan_tables = []   # (schema, table, comment)
    plan_cols = []     # (schema, table, column, comment)

    for schema, table in tables:
        tu = table.upper()
        # 表级备注：权威字典 > 人工表注释 > 自动推导
        if tu in auth_tables:
            tbl_comment = auth_tables[tu]
        else:
            tbl_comment = TABLE_COMMENTS.get(table, "")
            if not tbl_comment:
                tbl_comment = auto_table_comment(table)
        if tbl_comment:
            plan_tables.append((schema, table, tbl_comment))

        # 字段级备注
        cols = con.execute(f"""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = '{schema}' AND table_name = '{table}'
            ORDER BY ordinal_position
        """).fetchall()
        for (col,) in cols:
            cu = col.upper()
            # 优先级：数据验证勘误 > 权威 DBA 字典 > 人工覆盖 > 自动推断
            comment = (AUTH_CORRECTIONS.get(tu, {}).get(cu, "")
                       or auth_fields.get(tu, {}).get(cu, "")
                       or MANUAL_OVERRIDE.get(f"{table}.{col}", "")
                       or MANUAL_OVERRIDE.get(f"{schema}.{table}.{col}", "")
                       or translate_field(col))
            # 排除系统元数据列
            if col in ("_etl_time", "_etl_source"):
                comment = "ETL同步元数据列"
            if comment:
                plan_cols.append((schema, table, col, comment))

    if not apply:
        print(f"计划表级备注: {len(plan_tables)} 条")
        print(f"计划字段级备注: {len(plan_cols)} 条")
        print("\n--- 样例 (前 30 条字段) ---")
        for schema, table, col, comment in plan_cols[:30]:
            print(f"  {schema}.{table}.{col}: {comment}")
        return

    # 写入
    applied_t = applied_c = 0
    for schema, table, comment in plan_tables:
        try:
            con.execute(f'COMMENT ON TABLE {schema}."{table}" IS \'{comment}\'')
            applied_t += 1
        except Exception as e:
            print(f"  [表错误] {schema}.{table}: {e}")
    for schema, table, col, comment in plan_cols:
        try:
            con.execute(f'COMMENT ON COLUMN {schema}."{table}"."{col}" IS \'{comment}\'')
            applied_c += 1
        except Exception as e:
            print(f"  [列错误] {schema}.{table}.{col}: {e}")

    con.close()
    print(f"✅ 完成: 表 {applied_t} 条, 字段 {applied_c} 条")


def auto_table_comment(table: str) -> str:
    """根据表名自动推断中文表说明"""
    up = table.upper()
    table_suffix = {
        "MT": "主表", "DT": "明细表", "DOC": "档案表",
    }
    table_prefix = {
        "SALEOUT": "销售出库", "SALENOTES": "销售单", "SALERTN": "销售退货",
        "PURORDER": "采购订单", "PURIN": "采购入库",
        "ANGLEBALANCE": "货位库存", "STORBALANCE": "仓库库存",
        "GOODSDOC": "商品档案", "CLIENTDOC": "客户档案", "STAFFDOC": "员工档案",
        "SUPPLYDOC": "供应商档案", "STOREHOUSE": "仓库档案", "BATCHCODE": "批次批号",
        "CONTACTDOC": "联系人", "BUSINESSDOC": "往来单位", "RETBILL": "零售单",
        "RETGOODS": "零售商品", "REQUEST": "要货申请", "TRBILL": "调拨单",
        "TRGOODS": "调拨商品", "IMEIHIS": "序列号追溯", "PGPRICE": "商品价格",
    }
    name = up
    # 去掉后缀得到业务词干
    stem = up
    for suf in ["MT", "DT", "DOC"]:
        if up.endswith(suf):
            stem = up[:-len(suf)]
            break
    # 反向查前缀
    for prefix, cn in table_prefix.items():
        if stem.startswith(prefix):
            core = cn
            if "MT" in up:
                return f"{core}主表"
            if "DT" in up:
                return f"{core}明细表"
            if "DOC" in up:
                return f"{core}档案表"
            return core
    return ""


if __name__ == "__main__":
    main()
