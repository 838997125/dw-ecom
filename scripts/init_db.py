#!/usr/bin/env python3
"""
init_db.py - 冷启动初始化 DuckDB 数仓结构

作用：
  在 data/duckdb/ecom.duckdb 创建与生产环境完全一致的 6 个 schema、
  72 张空表（raw 44 + dim 5 + agg 7 + ymss_* 16），不含任何业务数据。
  结构来源：sql/schema_full.sql（由生产库 EXPORT DATABASE 导出）。

特性：
  - 幂等：schema 用 IF NOT EXISTS；表用 CREATE OR REPLACE（仅初始化用）
  - 已有非空库默认拒绝执行，防止误删生产数据，需 --force 才继续

用法：
  python3 scripts/init_db.py                 # 仅在库不存在或为空时初始化
  python3 scripts/init_db.py --force         # 强制重建（会清空现有表！）
  python3 scripts/init_db.py --db /path/to/ecom.duckdb
"""
import argparse
import sys
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / 'sql' / 'schema_full.sql'


def get_db_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    with open(ROOT / 'config' / 'config.yaml', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    return (ROOT / cfg['duckdb']['db_path']).resolve()


def table_count(con) -> int:
    return con.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema IN ('raw','dim','agg','ymss_ding','ymss_dwd','ymss_dws')
          AND table_type='BASE TABLE'
    """).fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description='初始化 DuckDB 数仓结构')
    ap.add_argument('--db', help='DuckDB 文件路径（默认读 config/config.yaml）')
    ap.add_argument('--force', action='store_true', help='已有库也强制重建（清空数据）')
    args = ap.parse_args()

    if not SCHEMA_SQL.exists():
        print(f'[ERROR] 找不到结构脚本: {SCHEMA_SQL}', file=sys.stderr)
        sys.exit(1)

    db_path = get_db_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    existing = db_path.exists()
    con = duckdb.connect(str(db_path))
    n = table_count(con) if existing else 0

    if n > 0 and not args.force:
        print(f'[ABORT] {db_path} 已存在且含 {n} 张表。')
        print('        如确认要清空重建，加 --force；否则无需初始化。')
        con.close()
        sys.exit(2)

    import re
    sql = SCHEMA_SQL.read_text(encoding='utf-8')
    # CREATE SCHEMA -> IF NOT EXISTS，保证重复执行不报错
    sql = re.sub(r'CREATE SCHEMA ', 'CREATE SCHEMA IF NOT EXISTS ', sql)
    # --force：先按依赖反序删掉目标 schema 下所有表
    if args.force:
        old = con.execute("""
            SELECT table_schema, table_name FROM information_schema.tables
            WHERE table_schema IN ('raw','dim','agg','ymss_ding','ymss_dwd','ymss_dws')
              AND table_type='BASE TABLE'
        """).fetchall()
        for sch, tbl in old:
            con.execute(f'DROP TABLE IF EXISTS "{sch}"."{tbl}"')
        print(f'  --force: 已清空 {len(old)} 张旧表')
    # schema_full.sql 每条语句以 ';' 结尾，逐条执行以获得清晰报错
    stmts = [s.strip() for s in sql.split(';') if s.strip()]
    for stmt in stmts:
        con.execute(stmt)
    con.execute('CHECKPOINT')

    n_after = table_count(con)
    con.close()
    print(f'[OK] 初始化完成: {db_path}')
    print(f'     共创建 {n_after} 张表（raw/dim/agg/ymss_*），无业务数据。')
    print('     下一步：python3 -m src.cdc.cdc full --all  从 Oracle 拉取首批数据')


if __name__ == '__main__':
    main()
