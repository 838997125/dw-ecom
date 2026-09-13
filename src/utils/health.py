"""
health.py - 系统健康监控
检查同步任务、DuckDB 文件、Oracle 连通性、数据一致性

用法：
  python3 -m src.utils.health              # 全部检查
  python3 -m src.utils.health --check cdc  # 只检查 CDC 状态
  python3 -m src.utils.health --check db   # 只检查数据库
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from src.utils.db import OracleConn, DuckDBConn, resolve_secret

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('health')


def load_config():
    with open(ROOT / 'config' / 'config.yaml') as f:
        return yaml.safe_load(f)


class HealthChecker:
    """系统健康监控"""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.results = []  # [(check_name, status, detail)]
        self.db_path = ROOT / self.config['duckdb']['db_path']

    def run_all(self):
        """运行所有检查"""
        logger.info("===== 系统健康检查开始 =====")
        self.check_duckdb_file()
        self.check_oracle_conn()
        self.check_cdc_status()
        self.check_data_freshness()
        self.check_duckdb_size()
        self.check_audit_errors()
        self.report()
        return self.results

    # ============================================
    # 检查项
    # ============================================
    def check_duckdb_file(self):
        """检查 DuckDB 文件是否存在、可访问"""
        name = "DuckDB 文件"
        if not self.db_path.exists():
            self._fail(name, f"文件不存在: {self.db_path}")
            return

        size_mb = self.db_path.stat().st_size / 1024 / 1024
        if size_mb < 0.1:
            self._fail(name, f"文件异常小: {size_mb:.2f}MB")
        elif size_mb > 10000:
            self._warn(name, f"文件过大: {size_mb:.1f}MB，建议 VACUUM")
        else:
            self._pass(name, f"{size_mb:.1f}MB")

        # 检查锁文件
        wal_file = str(self.db_path) + '.wal'
        if os.path.exists(wal_file):
            wal_size = os.path.getsize(wal_file) / 1024 / 1024
            if wal_size > 500:
                self._warn(name, f"WAL 文件较大: {wal_size:.1f}MB，可能有未提交事务")

    def check_oracle_conn(self):
        """检查 Oracle 连通性"""
        name = "Oracle 连接"
        try:
            o = self.config['oracle']
            conn = OracleConn(
                host=o['host'], port=o['port'], service_name=o['service_name'],
                username=o['username'], password=resolve_secret(o.get('password', ''), 'ORACLE_PASSWORD'),
                schema=o.get('schema', 'RACE')
            )
            version = conn.conn.version
            conn.close()
            self._pass(name, f"连接成功 | {o['host']} | {version}")
        except Exception as e:
            self._fail(name, f"连接失败: {e}")

    def check_cdc_status(self):
        """检查 CDC 同步状态"""
        name = "CDC 同步状态"
        try:
            db = DuckDBConn(str(self.db_path), read_only=True)
            rows = db.fetchall("""
                SELECT table_name, status, last_sync_time, sync_mode
                FROM raw._cdc_state
                ORDER BY last_sync_time DESC
            """)

            if not rows:
                self._warn(name, "无同步记录")
                db.close()
                return

            error_tables = [r for r in rows if r[1] == 'ERROR']
            old_tables = []
            for r in rows:
                if r[2]:
                    sync_time = r[2]
                    if isinstance(sync_time, str):
                        sync_time = datetime.fromisoformat(sync_time.replace('Z',''))
                    age = datetime.now() - sync_time
                    if age > timedelta(hours=6):
                        old_tables.append((r[0], str(age)))

            if error_tables:
                self._fail(name, f"{len(error_tables)} 张表同步失败: {', '.join(r[0] for r in error_tables)}")
            elif old_tables:
                self._warn(name, f"{len(old_tables)} 张表超过6小时未同步: {old_tables[0][0]} ({old_tables[0][1]})")
            else:
                self._pass(name, f"{len(rows)} 张表状态正常")

            db.close()
        except Exception as e:
            self._fail(name, f"检查失败: {e}")

    def check_data_freshness(self):
        """检查核心表数据时效性"""
        name = "数据时效性"
        try:
            db = DuckDBConn(str(self.db_path), read_only=True)

            # 检查 SALEOUTMT 最新数据时间
            row = db.fetchone("""
                SELECT MAX(TRY_CAST(DATES AS TIMESTAMP))
                FROM raw.SALEOUTMT
            """)
            if row and row[0]:
                age = datetime.now() - row[0]
                if age > timedelta(hours=2):
                    self._warn(name, f"SALEOUTMT 最新数据: {row[0]} ({age} 前)")
                else:
                    self._pass(name, f"SALEOUTMT 最新数据: {row[0]} ({age} 前)")
            else:
                self._warn(name, "SALEOUTMT 无数据")

            db.close()
        except Exception as e:
            self._warn(name, f"检查失败: {e}")

    def check_duckdb_size(self):
        """检查 DuckDB 数据量"""
        name = "DuckDB 数据量"
        try:
            db = DuckDBConn(str(self.db_path), read_only=True)
            row = db.fetchone("""
                SELECT COUNT(*) FROM (
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'raw' AND table_name != '_cdc_state'
                )
            """)
            table_count = row[0] if row else 0

            total_rows = 0
            tables = db.fetchall("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'raw' AND table_name != '_cdc_state'
            """)
            for (t,) in tables:
                try:
                    cnt = db.fetchone(f'SELECT COUNT(*) FROM raw."{t}"')[0]
                    total_rows += cnt
                except:
                    pass

            db.close()

            if total_rows < 100:
                self._warn(name, f"{table_count} 张表, {total_rows:,} 行 (数据量偏低)")
            elif total_rows > 10000000:
                self._warn(name, f"{table_count} 张表, {total_rows:,} 行 (数据量大,考虑归档)")
            else:
                self._pass(name, f"{table_count} 张表, {total_rows:,} 行")

        except Exception as e:
            self._fail(name, f"检查失败: {e}")

    def check_audit_errors(self):
        """检查数据对账记录"""
        name = "数据对账"
        try:
            db = DuckDBConn(str(self.db_path), read_only=True)
            row = db.fetchone("""
                SELECT COUNT(*) FROM agg.data_audit
                WHERE audit_date = CURRENT_DATE AND status IN ('MISMATCH', 'ERROR')
            """)
            error_count = row[0] if row else 0

            if error_count > 0:
                self._fail(name, f"今日有 {error_count} 条对账异常")
            else:
                ok_count = db.fetchone("""
                    SELECT COUNT(*) FROM agg.data_audit
                    WHERE audit_date = CURRENT_DATE AND status = 'OK'
                """)[0]
                self._pass(name, f"今日 {ok_count} 条对账正常, 0 条异常")

            db.close()
        except Exception as e:
            self._warn(name, f"检查失败: {e}")

    # ============================================
    # 辅助方法
    # ============================================
    def _pass(self, name, detail):
        self.results.append((name, 'PASS', detail))
        logger.info(f"✅ {name}: {detail}")

    def _warn(self, name, detail):
        self.results.append((name, 'WARN', detail))
        logger.warning(f"⚠️  {name}: {detail}")

    def _fail(self, name, detail):
        self.results.append((name, 'FAIL', detail))
        logger.error(f"❌ {name}: {detail}")

    def report(self):
        """汇总报告"""
        logger.info("===== 健康检查报告 =====")
        pass_count = sum(1 for _, s, _ in self.results if s == 'PASS')
        warn_count = sum(1 for _, s, _ in self.results if s == 'WARN')
        fail_count = sum(1 for _, s, _ in self.results if s == 'FAIL')
        logger.info(f"通过: {pass_count} | 警告: {warn_count} | 失败: {fail_count}")
        if fail_count > 0:
            logger.error("系统存在严重问题，需要处理！")
        elif warn_count > 0:
            logger.warning("系统有告警，建议关注。")
        else:
            logger.info("系统运行正常 ✅")


def main():
    parser = argparse.ArgumentParser(description='系统健康监控')
    parser.add_argument('--check', choices=['cdc', 'db', 'all'], default='all',
                        help='检查项: cdc=同步状态, db=数据库, all=全部')
    args = parser.parse_args()

    checker = HealthChecker()
    if args.check == 'all':
        checker.run_all()
    elif args.check == 'cdc':
        checker.check_cdc_status()
        checker.check_data_freshness()
        checker.report()
    elif args.check == 'db':
        checker.check_duckdb_file()
        checker.check_oracle_conn()
        checker.check_duckdb_size()
        checker.report()


if __name__ == '__main__':
    main()
