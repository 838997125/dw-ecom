import yaml, oracledb, duckdb, os, subprocess, py_compile, sys
sys.path.insert(0, ".")
from src.utils.db import resolve_secret
from datetime import datetime

with open('config/config.yaml') as f:
    config = yaml.safe_load(f)
o = config['oracle']
pw = resolve_secret(o.get('password',''), 'ORACLE_PASSWORD')
ora = oracledb.connect(user=o['username'], password=pw, dsn=f"{o['host']}:{o['port']}/{o['service_name']}")
duck = duckdb.connect('data/duckdb/ecom.duckdb', read_only=True)

print("=" * 70)
print("全面检查开始")
print("=" * 70)

# === 1. 表覆盖检查 ===
print("\n### 1. 表覆盖检查 ###")
cur = ora.cursor()
cur.execute("SELECT table_name FROM all_tables WHERE owner='RACE' ORDER BY table_name")
ora_tables = set(r[0] for r in cur.fetchall())
cur.close()

duck_tables = set(r[0] for r in duck.execute("""
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='raw' AND table_name != '_cdc_state'
""").fetchall())

missing_in_duck = ora_tables - duck_tables
extra_in_duck = duck_tables - ora_tables
print(f"Oracle: {len(ora_tables)} 张表, DuckDB raw: {len(duck_tables)} 张表")
if missing_in_duck:
    print(f"❌ Oracle 有但 DuckDB 没有: {missing_in_duck}")
else:
    print("✅ Oracle 所有表都已同步到 DuckDB")
if extra_in_duck:
    print(f"ℹ️  DuckDB 多出的表: {extra_in_duck}")

# === 2. _cdc_state 覆盖检查 ===
print("\n### 2. CDC 状态覆盖检查 ###")
cdc_tables = set(r[0] for r in duck.execute("SELECT table_name FROM raw._cdc_state").fetchall())
missing_cdc = duck_tables - cdc_tables
if missing_cdc:
    print(f"❌ DuckDB 有表但 _cdc_state 没有记录: {missing_cdc}")
else:
    print(f"✅ _cdc_state 覆盖全部 {len(duck_tables)} 张表")

# === 3. 逐表行数对比 ===
print("\n### 3. 逐表行数对比 ###")
cur = ora.cursor()
issues = []
for t in sorted(duck_tables):
    duck_cnt = duck.execute(f'SELECT COUNT(*) FROM raw."{t}"').fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM RACE.{t}")
    ora_cnt = cur.fetchone()[0]
    
    diff = ora_cnt - duck_cnt
    pct = (diff / ora_cnt * 100) if ora_cnt > 0 else 0
    
    if diff == 0:
        status = "✅"
    elif pct < 1 and diff < 100:
        status = "⚠️ "
    else:
        status = "❌"
        issues.append((t, ora_cnt, duck_cnt, diff, pct))
    
    print(f"  {status} {t:<25} Oracle={ora_cnt:>10,}  DuckDB={duck_cnt:>10,}  差={diff:>8,} ({pct:+.2f}%)")
cur.close()

if issues:
    print(f"\n❌ 有 {len(issues)} 张表存在显著差异:")
    for t, oc, dc, d, p in issues:
        print(f"   {t}: Oracle={oc:,} DuckDB={dc:,} 差={d:,} ({p:+.2f}%)")
else:
    print("\n✅ 所有表行数一致或差异极小")

# === 4. CDC 同步状态检查 ===
print("\n### 4. CDC 同步状态检查 ###")
cdc_rows = duck.execute("""
    SELECT table_name, status, last_sync_time, sync_mode
    FROM raw._cdc_state
    ORDER BY table_name
""").fetchall()

error_tables = []
stale_tables = []
for r in cdc_rows:
    tn, status, last_sync, mode = r
    if status == 'ERROR':
        error_tables.append((tn, last_sync))

if error_tables:
    print(f"❌ 同步出错: {len(error_tables)} 张表")
    for tn, ls in error_tables:
        print(f"   {tn}: 最后同步 {ls}")
else:
    print("✅ 无 ERROR 状态")

# === 5. crontab 覆盖检查 ===
print("\n### 5. crontab 定时任务覆盖检查 ###")
result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
cron_tables = set()
for line in result.stdout.split('\n'):
    if 'incremental --table' in line or 'full --table' in line:
        parts = line.split('--table')
        if len(parts) > 1:
            cron_tables.add(parts[1].strip().split()[0])

detail_tables = {'SALEOUTDT', 'SALENOTESDT', 'PURORDERDT', 'PURINDT', 'K_D3OMS_ORDERREFUNDDT'}
need_cron = duck_tables - cron_tables - detail_tables
if need_cron:
    print(f"❌ 需要定时同步但没配 cron: {need_cron}")
else:
    print(f"✅ 所有需要定时同步的表都已配置")

print(f"   定时同步: {len(cron_tables)} 张表")
print(f"   随主表联动(无需独立cron): {len(detail_tables)} 张表 ({detail_tables})")

# === 6. DuckDB 文件检查 ===
print("\n### 6. 数据库文件检查 ###")
db_path = 'data/duckdb/ecom.duckdb'
size_mb = os.path.getsize(db_path) / 1024 / 1024
wal_path = db_path + '.wal'
wal_exists = os.path.exists(wal_path)
wal_mb = os.path.getsize(wal_path) / 1024 / 1024 if wal_exists else 0
print(f"   DuckDB: {size_mb:.1f} MB")
if wal_exists:
    print(f"   WAL: {wal_mb:.1f} MB")
    if wal_mb > 500:
        print("   ⚠️  WAL 文件较大，可能有未提交事务")
else:
    print("   WAL: 无")

# === 7. Streamlit 锁冲突检查 ===
print("\n### 7. Streamlit 锁冲突检查 ###")
log_dir = 'logs/cron'
lock_errors = 0
if os.path.exists(log_dir):
    for f in os.listdir(log_dir):
        if f.endswith('.log'):
            try:
                with open(os.path.join(log_dir, f)) as fh:
                    lines = fh.read().strip().split('\n')
                    recent = [l for l in lines[-50:] if 'Conflicting lock' in l]
                    if recent:
                        lock_errors += len(recent)
                        print(f"   ⚠️  {f}: 最近有 {len(recent)} 条锁冲突")
            except:
                pass
if lock_errors == 0:
    print("✅ 近期无锁冲突错误")

# === 8. 配置一致性检查 ===
print("\n### 8. 配置文件一致性检查 ###")
with open('config/config.yaml') as f:
    cfg1 = yaml.safe_load(f)
with open('config/cdc_config.yaml') as f:
    cfg2 = yaml.safe_load(f)

inc1 = set(t['table'] for t in cfg1.get('cdc', {}).get('incremental', []))
inc2 = set(t['table'] for t in cfg2.get('cdc', {}).get('incremental', []))
if inc1 != inc2:
    print(f"⚠️  config.yaml 和 cdc_config.yaml 增量配置不一致:")
    print(f"   config.yaml only: {inc1 - inc2}")
    print(f"   cdc_config.yaml only: {inc2 - inc1}")
else:
    print(f"✅ 两个配置文件增量表一致 ({len(inc1)} 张表)")

# === 9. 仪表盘代码语法检查 ===
print("\n### 9. app.py 语法检查 ###")
try:
    py_compile.compile('app.py', doraise=True)
    print("✅ app.py 语法正确")
except py_compile.PyCompileError as e:
    print(f"❌ app.py 语法错误: {e}")

# === 10. K_PRN_SENDLOG 增量字段格式检查 ===
print("\n### 10. K_PRN_SENDLOG CREATETIME 格式检查 ###")
cur = ora.cursor()
cur.execute("SELECT CREATETIME FROM RACE.K_PRN_SENDLOG FETCH FIRST 5 ROWS ONLY")
samples = [s[0] for s in cur.fetchall()]
print(f"   Oracle CREATETIME 示例: {samples}")
cur.execute("""
    SELECT COUNT(*) FROM RACE.K_PRN_SENDLOG 
    WHERE CREATETIME NOT LIKE '%-%'
""")
bad_format = cur.fetchone()[0]
cur.close()
if bad_format > 0:
    print(f"❌ {bad_format} 行 CREATETIME 格式不是 YYYY-MM-DD HH:MI:SS, 增量同步会失败")
else:
    print("✅ CREATETIME 格式正常")

# === 11. IMEIHIS 增量字段格式检查 ===
print("\n### 11. IMEIHIS DATES 格式检查 ###")
cur = ora.cursor()
cur.execute("SELECT DATES FROM RACE.IMEIHIS FETCH FIRST 5 ROWS ONLY")
samples = [s[0] for s in cur.fetchall()]
print(f"   Oracle DATES 示例: {samples}")
cur.execute("SELECT COUNT(*) FROM RACE.IMEIHIS WHERE DATES IS NULL OR LENGTH(TRIM(DATES)) < 8")
null_dates = cur.fetchone()[0]
cur.close()
if null_dates > 0:
    print(f"⚠️  {null_dates} 行 DATES 为空或格式异常")
else:
    print("✅ DATES 格式正常")

# === 12. Streamlit 服务状态 ===
print("\n### 12. Streamlit 服务状态 ###")
result = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://localhost:8501'], capture_output=True, text=True)
if result.stdout == '200':
    print("✅ Streamlit 运行中 (HTTP 200)")
else:
    print(f"❌ Streamlit 异常 (HTTP {result.stdout})")

# === 13. LaunchAgent 状态 ===
print("\n### 13. LaunchAgent 开机自启检查 ###")
plist_path = os.path.expanduser('~/Library/LaunchAgents/com.openclaw.dw-ecom-streamlit.plist')
if os.path.exists(plist_path):
    print("✅ LaunchAgent 已配置")
else:
    print("❌ LaunchAgent 不存在")
result = subprocess.run(['launchctl', 'list'], capture_output=True, text=True)
if 'dw-ecom-streamlit' in result.stdout:
    print("✅ LaunchAgent 已加载")
else:
    print("⚠️  LaunchAgent 未加载")

ora.close()
duck.close()

print("\n" + "=" * 70)
print("检查完毕")
print("=" * 70)
