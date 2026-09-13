#!/usr/bin/env python3
"""
compact_db.py - 彻底压缩 DuckDB 数据库（EXPORT/IMPORT 方案，带写锁保护）

高频 CDC 的 DELETE+INSERT 导致严重的存储碎片。
通过 EXPORT DATABASE + IMPORT DATABASE 重建，完全保留 schema（约束/类型）。
整个过程持有操作系统写锁，防止 CDC 并发写入。

用法: python3 scripts/compact_db.py
"""
import sys
import time
import shutil
import os
import fcntl
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import WRITE_LOCK_PATH, WRITE_LOCK_TIMEOUT_SECONDS

EXPORT_DIR = '/tmp/ecom_db_export'

def acquire_write_lock():
    """获取写锁（与 CDC 共用同一把锁），返回文件描述符"""
    WRITE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = open(WRITE_LOCK_PATH, 'w')
    deadline = time.monotonic() + WRITE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fd.close()
                raise TimeoutError(f"等待写锁超时（{WRITE_LOCK_TIMEOUT_SECONDS}s）")
            time.sleep(2)

def release_write_lock(fd):
    if fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except:
            pass

def main():
    db_path = str(ROOT / 'data' / 'duckdb' / 'ecom.duckdb')
    backup_path = db_path + '.precompact'
    tmp_db = db_path + '.compact'

    src_size = os.path.getsize(db_path) / 1024 / 1024 / 1024
    print(f"[COMPACT] 原始数据库大小: {src_size:.2f} GB")

    # 获取写锁（阻塞直到 CDC 空闲）
    print("[COMPACT] 等待写锁（CDC 同步暂停）...")
    lock_fd = acquire_write_lock()
    print("[COMPACT] 写锁已获取")

    start = time.time()
    import duckdb

    try:
        # 1. 备份
        print(f"[COMPACT] 备份到 {backup_path} ...")
        shutil.copy2(db_path, backup_path)
        print("[COMPACT] 备份完成")

        # 2. EXPORT
        if os.path.exists(EXPORT_DIR):
            shutil.rmtree(EXPORT_DIR)
        os.makedirs(EXPORT_DIR)

        print("[COMPACT] EXPORT DATABASE ...")
        con = duckdb.connect(db_path, read_only=True)
        con.execute(f"EXPORT DATABASE '{EXPORT_DIR}' (FORMAT PARQUET)")
        con.close()

        # 3. IMPORT 到新库
        if os.path.exists(tmp_db):
            os.remove(tmp_db)

        print("[COMPACT] 创建新数据库并 IMPORT ...")
        t2 = time.time()
        con2 = duckdb.connect(tmp_db)
        con2.execute(f"IMPORT DATABASE '{EXPORT_DIR}'")
        con2.execute("CHECKPOINT")
        con2.close()
        print(f"[COMPACT] IMPORT 完成, 耗时 {time.time()-t2:.1f}s")

        # 4. 验证新库行数
        print("[COMPACT] 验证新数据库 ...")
        con3 = duckdb.connect(tmp_db, read_only=True)
        tables = con3.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('raw','agg','dim') AND table_type='BASE TABLE'
            ORDER BY table_schema, table_name
        """).fetchall()

        con_orig = duckdb.connect(db_path, read_only=True)
        all_ok = True
        for schema, table in tables:
            new_count = con3.execute(f'SELECT COUNT(*) FROM {schema}."{table}"').fetchone()[0]
            old_count = con_orig.execute(f'SELECT COUNT(*) FROM {schema}."{table}"').fetchone()[0]
            if new_count != old_count:
                print(f"  [MISMATCH] {schema}.{table}: old={old_count:,} new={new_count:,}")
                all_ok = False
        con3.close()
        con_orig.close()

        if not all_ok:
            print("[COMPACT] 行数校验失败，保留原库不变")
            os.remove(tmp_db)
            shutil.rmtree(EXPORT_DIR, ignore_errors=True)
            sys.exit(1)

        print(f"[COMPACT] {len(tables)} 张表行数校验通过")

        # 5. 替换原库
        print("[COMPACT] 替换原数据库 ...")
        os.remove(db_path)
        shutil.move(tmp_db, db_path)

        # 6. 清理
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)

        elapsed = time.time() - start
        dst_size = os.path.getsize(db_path) / 1024 / 1024 / 1024
        print(f"\n[COMPACT] 完成! 总耗时 {elapsed:.1f}s")
        print(f"[COMPACT] 压缩前: {src_size:.2f} GB")
        print(f"[COMPACT] 压缩后: {dst_size:.2f} GB")
        print(f"[COMPACT] 节省: {src_size - dst_size:.2f} GB ({(1-dst_size/src_size)*100:.1f}%)")
        print(f"[COMPACT] 备份文件: {backup_path}（确认无误后可删除）")

    except Exception as e:
        print(f"[COMPACT] 失败: {e}")
        import traceback
        traceback.print_exc()
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        sys.exit(1)
    finally:
        release_write_lock(lock_fd)

if __name__ == '__main__':
    main()
