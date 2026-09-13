#!/usr/bin/env python3
"""
safe_vacuum.py - 安全执行 DuckDB VACUUM

通过 DuckDBConn 获取写锁后执行 VACUUM，避免与 CDC 同步冲突。
用法: python3 scripts/safe_vacuum.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import DuckDBConn

def main():
    db_path = str(ROOT / 'data' / 'duckdb' / 'ecom.duckdb')
    print(f"[VACUUM] 连接 {db_path} ...")
    start = time.time()

    db = DuckDBConn(db_path)
    try:
        print("[VACUUM] 获取写锁成功，开始执行 VACUUM ...")
        # VACUUM ANALYZE 同时更新统计信息，对查询优化器有帮助
        db.conn.execute("VACUUM ANALYZE")
        elapsed = time.time() - start

        # 获取压缩后大小
        import os
        size_gb = os.path.getsize(db_path) / 1024 / 1024 / 1024
        print(f"[VACUUM] 完成! 耗时 {elapsed:.1f}s，当前数据库大小: {size_gb:.2f} GB")
    except Exception as e:
        print(f"[VACUUM] 失败: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == '__main__':
    main()
