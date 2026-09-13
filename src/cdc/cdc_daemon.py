#!/usr/bin/env python3
"""
cdc_daemon.py - 单进程 CDC 调度器

替代 31 个 crontab 条目：
  - 1 个持久 Oracle 连接（懒探活，断了立即重连）
  - 1 个持久 DuckDB 写连接（持有写锁，零争抢）
  - 内部调度表，串行执行，天然无锁冲突
  - SALEOUTMT 增量后立即触发 C 端零售聚合
  - 单表失败不阻塞其他表

用法：
  python3 -m src.cdc.cdc_daemon           # 前台运行
  python3 -m src.cdc.cdc_daemon --status  # 查看调度状态
"""
import argparse
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import OracleConn, DuckDBConn
from src.cdc.cdc import CDCSync

# 日志
LOG_DIR = ROOT / 'logs' / 'cron'
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] daemon - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / 'daemon.log', encoding='utf-8')
    ]
)
logger = logging.getLogger('daemon')

# ============================================
# 调度表
# ============================================
# (table, interval_seconds, mode, options)
# mode: 'incremental', 'incremental_id', 'full', 'agg'
# options: after=None (依赖触发), at=None (指定时间 HH:MM), weekday=None (0=Mon..6=Sun)

SCHEDULE = [
    # ---- 5 分钟：核心交易 ----
    ('SALEOUTMT',                 300, 'incremental', {}),
    ('SKWMS_JGM_W',               300, 'incremental_id', {}),
    ('K_D3OMS_STOCKOUTMT_PLAN',   300, 'incremental', {}),
    ('SALENOTESMT',               300, 'incremental', {}),
    ('K_D3OMS_ORDERREFUNDMT',     300, 'incremental', {}),

    # ---- 15 分钟 ----
    ('ANGLEBALANCE',              900, 'incremental', {}),
    ('IMEIHIS',                   900, 'incremental', {}),
    ('PURORDERMT',                900, 'incremental', {}),
    ('PURINMT',                   900, 'incremental', {}),
    ('REQUESTMT',                 900, 'incremental', {}),
    ('K_PRN_SENDLOG',             900, 'incremental', {}),

    # ---- 30 分钟 ----
    ('RETBILLMT',                1800, 'incremental', {}),

    # ---- 1 小时 ----
    ('SALERTNMT',                3600, 'incremental', {}),
    ('TRBILLMT',                 3600, 'incremental', {}),

    # ---- 6 小时：主数据增量（增量有效的） ----
    ('GOODSDOC',                21600, 'incremental', {}),
    ('SUPPLYDOC',               21600, 'incremental', {}),
    ('STOREHOUSE',              21600, 'incremental', {}),
    ('STAFFDOC',                21600, 'incremental', {}),

    # ---- 6 小时：全量（增量失效的小表） ----
    ('CLIENTDOC',               21600, 'full', {}),
    ('BUSINESSDOC',             21600, 'full', {}),
    ('GOODSATTR',               21600, 'full', {}),
    ('PGPRICE',                 21600, 'full', {}),
    ('CONTACTDOC',              21600, 'full', {}),

    # ---- 每日凌晨全量大表 ----
    ('BATCHCODE',               86400, 'full', {'at': '03:40'}),
    ('ZZERP_CK_KPD_W',          86400, 'full', {'at': '03:41'}),
    ('ZZERP_GT_KPD_W',          86400, 'full', {'at': '03:42'}),
    ('ZZERP_RK_KPD_W',          86400, 'full', {'at': '03:43'}),
    ('ZZERP_XT_KPD_W',          86400, 'full', {'at': '03:44'}),

    # ---- 每日 MySQL → DuckDB 全量同步（YMSS_DING/DWD/DWS 16张表） ----
    ('__mysql_full_sync__',     86400, 'mysql_full', {'at': '07:40'}),

    # ---- 周级全量（周日 03:30） ----
    ('SKWMS_JGM_W_full',       604800, 'full', {'at': '03:30', 'weekday': 6, 'table': 'SKWMS_JGM_W'}),

    # ---- 日级维护（由 daemon 独占写锁时执行，避免冲突） ----
    ('__verify_all__',          86400, 'verify', {'at': '02:00'}),
    ('__vacuum__',              86400, 'vacuum', {'at': '04:00'}),
    ('__compact__',            604800, 'compact', {'at': '04:30', 'weekday': 6}),
]

# 依赖触发：某表增量完成后立即执行
TRIGGERS = {
    'SALEOUTMT': ['__c_retail__'],   # 销售增量后立即刷新 C 端零售聚合
}

# 循环检查间隔（秒）
TICK_INTERVAL = 10


class CDCDaemon:
    def __init__(self):
        self.running = True
        self.cdc = None  # type: CDCSync
        self.last_run = {}       # key -> timestamp
        self.last_result = {}    # key -> (rows, elapsed, ok)
        self._init_cdc()

        # 信号处理
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _init_cdc(self):
        """初始化 CDCSync（Oracle 持久连接，DuckDB 每次用完即关释放写锁）"""
        self.cdc = CDCSync()
        # 立即关闭 DuckDB 连接，释放写锁；后续每个任务执行时重连
        self._release_duckdb()
        logger.info("Daemon initialized (Oracle persistent, DuckDB per-task)")

    def _acquire_duckdb(self):
        """任务执行前确保 DuckDB 连接可用"""
        if self.cdc.duckdb._conn is None:
            _ = self.cdc.duckdb.conn  # 触发连接

    def _release_duckdb(self):
        """任务执行后关闭 DuckDB 连接，释放写锁给只读服务"""
        try:
            if self.cdc.duckdb._conn is not None:
                self.cdc.duckdb.close()
        except Exception:
            pass
        self.cdc.duckdb._conn = None
        self.cdc.duckdb._write_lock = None

    def _shutdown(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    # ============================================
    # Oracle 懒探活
    # ============================================
    def _ensure_oracle(self):
        """每次查 Oracle 前探活，断了立即重连"""
        for attempt in range(3):
            try:
                conn = self.cdc.oracle.conn
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM DUAL")
                cur.close()
                return True
            except Exception as e:
                logger.warning(f"Oracle ping failed (attempt {attempt+1}/3): {e}")
                try:
                    self.cdc.oracle.close()
                except Exception:
                    pass
                self.cdc.oracle._conn = None
                if attempt < 2:
                    time.sleep(5)
        logger.error("Oracle reconnection failed after 3 attempts")
        return False

    # ============================================
    # 任务执行
    # ============================================
    def _run_task(self, key, table, mode):
        """执行单个同步任务（前后管理 DuckDB 写锁）"""
        t0 = time.time()
        rows = 0
        ok = True
        self._acquire_duckdb()
        try:
            if mode == 'incremental':
                if not self._ensure_oracle():
                    raise ConnectionError("Oracle unavailable")
                rows, _ = self.cdc.incremental_sync(table)
                logger.info(f"[INC] {table}: {rows:,} rows in {time.time()-t0:.1f}s")

            elif mode == 'incremental_id':
                if not self._ensure_oracle():
                    raise ConnectionError("Oracle unavailable")
                # ID 模式在 cdc.py 内部通过 cdc_mode=id 自动路由
                rows, _ = self.cdc.incremental_sync(table)
                logger.info(f"[INC-ID] {table}: {rows:,} rows in {time.time()-t0:.1f}s")

            elif mode == 'full':
                if not self._ensure_oracle():
                    raise ConnectionError("Oracle unavailable")
                rows, _ = self.cdc.full_sync(table)
                logger.info(f"[FULL] {table}: {rows:,} rows in {time.time()-t0:.1f}s")

            elif mode == 'verify':
                self._run_verify()
                logger.info(f"[VERIFY] done in {time.time()-t0:.1f}s")

            elif mode == 'vacuum':
                self._run_vacuum()
                logger.info(f"[VACUUM] done in {time.time()-t0:.1f}s")

            elif mode == 'compact':
                self._run_compact()
                logger.info(f"[COMPACT] done in {time.time()-t0:.1f}s")

            elif mode == 'mysql_full':
                self._run_mysql_full_sync()
                logger.info(f"[MYSQL-FULL] done in {time.time()-t0:.1f}s")

            elif mode == 'agg':
                self._run_c_retail()
                logger.info(f"[AGG] c_retail done in {time.time()-t0:.1f}s")

            # 触发依赖任务
            if table in TRIGGERS:
                for trigger_key in TRIGGERS[table]:
                    if rows > 0 or trigger_key == '__c_retail__':
                        logger.info(f"[TRIGGER] {table} -> {trigger_key}")
                        self._run_task(trigger_key, trigger_key, 'agg')

        except Exception as e:
            ok = False
            logger.error(f"[FAIL] {key}: {e}")
            logger.error(traceback.format_exc())
            # 尝试重连
            if 'Oracle' in str(e) or 'connection' in str(e).lower():
                self._ensure_oracle()
        finally:
            # 任务完成后立即释放 DuckDB 写锁，让 Streamlit 等只读服务能访问
            self._release_duckdb()

        elapsed = time.time() - t0
        self.last_result[key] = (rows, elapsed, ok)
        return rows, elapsed, ok

    def _run_c_retail(self):
        """C 端零售聚合，复用 daemon 的 DuckDB 连接"""
        from src.agg.refresh_c_retail import SQL_REBUILD, SQL_TRACE, SQL_INDEXES
        con = self.cdc.duckdb.conn
        con.execute(SQL_REBUILD)
        for sql in SQL_INDEXES:
            con.execute(sql)
        con.execute(SQL_TRACE)

    def _run_verify(self):
        """数据校验"""
        results = self.cdc.verify_all()
        for r in results:
            s = r.get('status', 'ERROR')
            if s != 'OK':
                logger.warning(f"[VERIFY] {r['table']}: {s} (Oracle={r.get('oracle')}, DuckDB={r.get('duckdb')})")

    def _run_vacuum(self):
        """VACUUM ANALYZE（daemon 持有写锁，直接执行）"""
        con = self.cdc.duckdb.conn
        logger.info("[VACUUM] Starting VACUUM ANALYZE...")
        con.execute("VACUUM ANALYZE")
        logger.info("[VACUUM] Done")

    def _run_mysql_full_sync(self):
        """MySQL → DuckDB 全量同步：释放写锁后子进程执行，完成后重连"""
        script = ROOT / 'scripts' / 'sync_mysql_to_duckdb.py'
        if not script.exists():
            logger.warning(f"[MYSQL-FULL] Script not found: {script}")
            return

        logger.info("[MYSQL-FULL] Closing DuckDB connection to release write lock...")
        try:
            self.cdc.duckdb.close()
        except Exception:
            pass
        self.cdc.duckdb._conn = None
        self.cdc.duckdb._write_lock = None

        logger.info("[MYSQL-FULL] Running sync subprocess...")
        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=600,
                cwd=str(ROOT)
            )
            if result.returncode != 0:
                logger.error(f"[MYSQL-FULL] Failed (rc={result.returncode}): {result.stderr[-1000:]}")
            else:
                lines = result.stdout.strip().split('\n')
                summary = next((l for l in reversed(lines) if '同步完成' in l or 'FAILED' in l), lines[-1] if lines else '')
                logger.info(f"[MYSQL-FULL] Done. {summary}")
        except subprocess.TimeoutExpired:
            logger.error("[MYSQL-FULL] Timed out after 600s")
        except Exception as e:
            logger.error(f"[MYSQL-FULL] Error: {e}")
        finally:
            logger.info("[MYSQL-FULL] Reconnecting DuckDB...")
            self.cdc.duckdb = DuckDBConn(str(ROOT / self.cdc.config['duckdb']['db_path']))
            logger.info("[MYSQL-FULL] Reconnected")

    def _run_compact(self):
        """周级压缩：必须先关闭 DuckDB 连接释放写锁，子进程才能拿到锁"""
        import subprocess
        script = ROOT / 'scripts' / 'compact_db.py'
        if not script.exists():
            logger.warning(f"[COMPACT] Script not found: {script}")
            return

        logger.info("[COMPACT] Closing DuckDB connection to release write lock...")
        try:
            self.cdc.duckdb.close()
        except Exception:
            pass
        self.cdc.duckdb._conn = None

        logger.info("[COMPACT] Running compaction subprocess...")
        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=600,
                cwd=str(ROOT)
            )
            if result.returncode != 0:
                logger.error(f"[COMPACT] Failed (rc={result.returncode}): {result.stderr[-500:]}")
            else:
                logger.info(f"[COMPACT] Done. {result.stdout[-200:]}")
        finally:
            # 无论成功失败都重连
            logger.info("[COMPACT] Reconnecting DuckDB...")
            self.cdc.duckdb = DuckDBConn(str(ROOT / self.cdc.config['duckdb']['db_path']))
            logger.info("[COMPACT] Reconnected")

    # ============================================
    # 调度判断
    # ============================================
    def _should_run(self, key, interval, options):
        """判断任务是否到点"""
        now = datetime.now()
        last = self.last_run.get(key, 0)
        elapsed = time.time() - last

        # 指定时间的任务
        at_time = options.get('at')
        if at_time:
            target_h, target_m = map(int, at_time.split(':'))
            weekday = options.get('weekday')

            # 检查今天是否该跑
            if weekday is not None and now.weekday() != weekday:
                return False

            # 在目标时间之后、且距上次运行 > 12 小时（防重复）
            target_today = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if now >= target_today and elapsed > 43200:
                return True
            return False

        # 间隔任务
        return elapsed >= interval

    def _get_priority_key(self, item):
        """计算调度优先级：到点的紧急程度"""
        key = item[0]
        interval = item[1]
        options = item[3] if len(item) > 3 else {}
        last = self.last_run.get(key, 0)
        elapsed = time.time() - last

        at_time = options.get('at')
        if at_time:
            target_h, target_m = map(int, at_time.split(':'))
            now = datetime.now()
            weekday = options.get('weekday')
            if weekday is not None and now.weekday() != weekday:
                return float('inf')
            target_today = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if now >= target_today and elapsed > 43200:
                return 0
            return float('inf')

        overshoot = elapsed - interval
        if overshoot >= 0:
            return overshoot
        return float('inf')

    # ============================================
    # 主循环
    # ============================================
    def run(self):
        logger.info("=" * 60)
        logger.info("CDC Daemon started")
        logger.info(f"  {len(SCHEDULE)} tasks scheduled")
        logger.info(f"  Tick interval: {TICK_INTERVAL}s")
        logger.info("=" * 60)

        # 初始化 last_run：
        # - 间隔型任务：设为 0，启动后立即跑第一轮
        # - 指定时间型任务（at=HH:MM）：设为当前时间，等到下一个调度点再跑
        #   避免 daemon 重启时在白天误触发凌晨全量任务
        now = time.time()
        for item in SCHEDULE:
            key, options = item[0], item[3] if len(item) > 3 else {}
            if options.get('at'):
                self.last_run[key] = now  # 不立即触发，等下一个调度点

        # 启动时立即跑一轮高频任务
        bootstrap = [
            ('SALEOUTMT', 'incremental'),
            ('SKWMS_JGM_W', 'incremental_id'),
            ('K_D3OMS_STOCKOUTMT_PLAN', 'incremental'),
            ('K_D3OMS_ORDERREFUNDMT', 'incremental'),
            ('SALENOTESMT', 'incremental'),
        ]
        for table, mode in bootstrap:
            self._run_task(table, table, mode)
            self.last_run[table] = time.time()

        while self.running:
            try:
                # 找最该跑的任务
                candidates = []
                for item in SCHEDULE:
                    key, table, interval, mode, options = (
                        item[0], item[0], item[1], item[2], item[3] if len(item) > 3 else {}
                    )
                    if self._should_run(key, interval, options):
                        candidates.append((self._get_priority_key(item), item))

                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    _, item = candidates[0]
                    key = item[0]
                    interval = item[1]
                    mode = item[2]
                    options = item[3] if len(item) > 3 else {}
                    table = options.get('table', key)
                    self._run_task(key, table, mode)
                    self.last_run[key] = time.time()
                else:
                    # 没有到点的任务，短暂休眠
                    time.sleep(TICK_INTERVAL)

            except Exception as e:
                logger.error(f"Main loop error: {e}")
                logger.error(traceback.format_exc())
                time.sleep(30)

        self._shutdown_cleanup()

    def _shutdown_cleanup(self):
        logger.info("Closing connections...")
        try:
            if self.cdc:
                self.cdc.close()
        except Exception:
            pass
        logger.info("Daemon stopped")

    def status(self):
        """打印调度状态"""
        print(f"\n{'任务':<30}{'模式':<16}{'间隔':>8}{'上次运行':>20}{'耗时':>8}{'行数':>10}  状态")
        print("-" * 110)
        now = time.time()
        for item in SCHEDULE:
            key, interval, mode = item[0], item[1], item[2]
            options = item[3] if len(item) > 3 else {}
            last_ts = self.last_run.get(key, 0)
            last_str = datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M:%S') if last_ts else 'never'
            result = self.last_result.get(key, (0, 0, None))
            rows, elapsed, ok = result
            status = '✅' if ok else ('❌' if ok is False else '⏳')
            interval_str = f"{interval}s" if interval < 3600 else f"{interval//3600}h"
            if options.get('at'):
                interval_str = f"@{options['at']}"
                if options.get('weekday') is not None:
                    days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
                    interval_str += f" {days[options['weekday']]}"
            print(f"{key:<30}{mode:<16}{interval_str:>8}{last_str:>20}{elapsed:>7.1f}s{rows:>10,}  {status}")
        print()


def main():
    parser = argparse.ArgumentParser(description='CDC Daemon')
    parser.add_argument('--status', action='store_true', help='Show status and exit')
    args = parser.parse_args()

    daemon = CDCDaemon()

    if args.status:
        daemon.status()
        daemon.cdc.close()
        return

    daemon.run()


if __name__ == '__main__':
    main()
