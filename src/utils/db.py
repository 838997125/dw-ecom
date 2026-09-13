"""
db.py - 数据库连接管理
Oracle + DuckDB 双连接

写入串行化说明（2026-07-31）：
  DuckDB 是单进程写模型。多个 CDC cron 任务并发启动时会抢写锁报错
  （Conflicting lock is held ...）。为避免这种情况，DuckDBConn 在以读写模式
  打开数据库时会先拿一个操作系统级的文件锁（fcntl.flock），拿到后再连
  DuckDB，close() 时自动释放。

  只读连接（app.py 查询台 read_only=True）不应使用本类，或可以直接用
  duckdb.connect(read_only=True) 绕过写锁，完全不受影响。
"""
import os
import sys
import fcntl
import time
import oracledb
import duckdb
import logging
from pathlib import Path

# 启动早期加载项目根目录 .env（零依赖），让 ORACLE_PASSWORD / MYSQL_PASSWORD 等生效
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
try:
    from envloader import load_env
    load_env(_PROJECT_ROOT / '.env')
except Exception:
    pass

logger = logging.getLogger(__name__)


def resolve_secret(value, env_name=None):
    """配置密钥解析：支持三种写法，优先级如下。
    1. 形如 ``${ENV_VAR}`` 的占位符 → 读对应环境变量；
    2. 显式传入了 env_name 且该环境变量存在 → 用环境变量；
    3. 否则原样返回 config 里的明文（部署时建议用占位符，避免密码进 git）。
    """
    if isinstance(value, str) and value.strip().startswith('${') and value.strip().endswith('}'):
        env_key = value.strip()[2:-1].strip()
        return os.environ.get(env_key, '')
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return value


# 全局写锁文件：所有 DuckDB 写入进程共用同一把锁
# 默认放 /tmp（重启自动清理，不影响 .duckdb 文件），可用 ECOM_WRITE_LOCK 覆盖
WRITE_LOCK_PATH = Path(os.environ.get('ECOM_WRITE_LOCK', '/tmp/ecom_duckdb_write.lock'))

# 等待锁的最长秒数（超过就放弃，让下一轮 cron 重试）
# SALEOUTMT 单次跑 17s 左右，给 60s 足够排一轮队
WRITE_LOCK_TIMEOUT_SECONDS = 60


class _WriteLock:
    """操作系统级文件锁，串行化所有 DuckDB 写进程。"""

    def __init__(self, lock_path: Path, timeout: float):
        self.lock_path = lock_path
        self.timeout = timeout
        self._fd = None

    def acquire(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.lock_path, 'w')
        deadline = time.monotonic() + self.timeout
        wait_logged = False
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if wait_logged:
                    logger.info("DuckDB 写锁已获得，开始执行")
                return
            except BlockingIOError:
                if not wait_logged:
                    logger.info(f"DuckDB 写锁被占用，最多等待 {self.timeout:.0f}s...")
                    wait_logged = True
                if time.monotonic() >= deadline:
                    self._fd.close()
                    self._fd = None
                    raise TimeoutError(
                        f"等待 DuckDB 写锁超时（{self.timeout:.0f}s），放弃本轮"
                    )
                time.sleep(1)

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


class OracleConn:
    """Oracle 连接管理器"""

    def __init__(self, host: str, port: int, service_name: str,
                 username: str, password: str, schema: str = "RACE"):
        self.dsn = f"{host}:{port}/{service_name}"
        self.username = username
        self.password = password
        self.schema = schema
        self._conn = None

    # Oracle 单次调用超时（毫秒），防止网络抖动导致进程永久卡死、持有 DuckDB 写锁
    CALL_TIMEOUT_MS = 180_000  # 3 分钟

    @property
    def conn(self):
        if self._conn is None:
            self._conn = oracledb.connect(
                user=self.username,
                password=self.password,
                dsn=self.dsn
            )
            self._conn.call_timeout = self.CALL_TIMEOUT_MS
            logger.info(f"Oracle 连接成功 | {self.dsn} | DB版本: {self._conn.version} | call_timeout={self.CALL_TIMEOUT_MS}ms")
        return self._conn

    def execute(self, sql: str, params: dict = None):
        cur = self.conn.cursor()
        cur.execute(sql, params or {})
        return cur

    def fetchall(self, sql: str, params: dict = None):
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows

    def fetch_dataframe(self, sql: str, params: dict = None):
        """返回列名 + 行数据"""
        cur = self.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
        cur.close()
        return cols, rows

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except:
                pass
            logger.info("Oracle 连接关闭")


class DuckDBConn:
    """DuckDB 连接管理器（读写模式，自动加全局写锁）"""

    def __init__(self, db_path: str, read_only: bool = False,
                 lock_timeout: float = WRITE_LOCK_TIMEOUT_SECONDS):
        self.db_path = db_path
        self.read_only = read_only
        self.lock_timeout = lock_timeout
        self._conn = None
        self._write_lock = None

    @property
    def conn(self):
        if self._conn is None:
            # 只读模式不加锁，直接连
            if self.read_only:
                self._conn = duckdb.connect(self.db_path, read_only=True)
                logger.info(f"DuckDB 只读连接成功 | {self.db_path}")
                return self._conn
            # 读写模式：先抢全局写锁，再连 DuckDB
            self._write_lock = _WriteLock(WRITE_LOCK_PATH, self.lock_timeout)
            self._write_lock.acquire()
            try:
                self._conn = duckdb.connect(self.db_path)
            except Exception:
                # DuckDB 连接失败就释放锁，避免占着茅坑不拉屎
                self._write_lock.release()
                self._write_lock = None
                raise
            logger.info(f"DuckDB 连接成功 | {self.db_path}")
        return self._conn

    def execute(self, sql: str, params=None):
        return self.conn.execute(sql, params or [])

    def fetchall(self, sql: str, params=None):
        return self.conn.execute(sql, params or []).fetchall()

    def fetchone(self, sql: str, params=None):
        return self.conn.execute(sql, params or []).fetchone()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("DuckDB 连接关闭")
        # 锁在连接关闭后才释放，确保整个事务期间都持锁
        if self._write_lock is not None:
            self._write_lock.release()
            self._write_lock = None


def load_config(config_path: str = None) -> dict:
    """加载 YAML 配置"""
    import yaml
    if config_path is None:
        # 默认路径相对于项目根目录
        root = Path(__file__).parent.parent
        config_path = root / "config" / "config.yaml"
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def get_oracle(config: dict = None) -> OracleConn:
    """快捷获取 Oracle 连接"""
    if config is None:
        config = load_config()
    o = config['oracle']
    return OracleConn(
        host=o['host'], port=o['port'], service_name=o['service_name'],
        username=o['username'],
        password=resolve_secret(o.get('password', ''), 'ORACLE_PASSWORD'),
        schema=o.get('schema', 'RACE')
    )


def get_duckdb(config: dict = None) -> DuckDBConn:
    """快捷获取 DuckDB 连接"""
    if config is None:
        config = load_config()
    root = Path(__file__).parent.parent
    db_path = root / config['duckdb']['db_path']
    return DuckDBConn(str(db_path))
