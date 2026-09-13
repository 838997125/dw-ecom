import yaml, oracledb, duckdb, sys
sys.path.insert(0, ".")
from src.utils.db import resolve_secret

with open('config/config.yaml') as f:
    config = yaml.safe_load(f)
o = config['oracle']

ora = oracledb.connect(user=o['username'], password=resolve_secret(o.get('password',''), 'ORACLE_PASSWORD'), dsn=f"{o['host']}:{o['port']}/{o['service_name']}")
duck = duckdb.connect('data/duckdb/ecom.duckdb', read_only=True)

print('='*70)
print('1. IMEIHIS -- Oracle 17950, DuckDB 3223, 差 82%')
print('='*70)

cur = ora.cursor()
cur.execute('SELECT COUNT(*) FROM RACE.IMEIHIS')
total_ora = cur.fetchone()[0]
cur.execute('SELECT COUNT(DISTINCT SERIALNUM) FROM RACE.IMEIHIS')
distinct_ora = cur.fetchone()[0]
print(f'Oracle: 总行 {total_ora}, 去重 SERIALNUM 后 {distinct_ora}, 重复行 {total_ora - distinct_ora}')

cur.execute('''
    SELECT SERIALNUM, COUNT(*) as cnt
    FROM RACE.IMEIHIS
    GROUP BY SERIALNUM
    HAVING COUNT(*) > 1
    ORDER BY cnt DESC
    FETCH FIRST 5 ROWS ONLY
''')
dups = cur.fetchall()
print(f'重复 SERIALNUM: {len(dups)} 组 (仅取前5)')
for r in dups:
    print(f'  SERIALNUM={r[0]} 出现 {r[1]} 次')

duck_total = duck.execute('SELECT COUNT(*) FROM raw.IMEIHIS').fetchone()[0]
duck_distinct = duck.execute('SELECT COUNT(DISTINCT SERIALNUM) FROM raw.IMEIHIS').fetchone()[0]
print(f'DuckDB: 总行 {duck_total}, 去重后 {duck_distinct}')

# 看 IMEIHIS 重复的行内容是否真的不同
if dups:
    sn = dups[0][0]
    cur.execute(f'SELECT SERIALNUM, IMEINO, DATES, GOODSID, IMEISTATE, NEWSTATE FROM RACE.IMEIHIS WHERE SERIALNUM = {sn}')
    rows = cur.fetchall()
    print(f'\n抽查 SERIALNUM={sn} 的 {len(rows)} 行:')
    for r in rows:
        print(f'  {r}')

cur.close()

print()
print('='*70)
print('2. GOODSATTR -- Oracle 51441, DuckDB 49511, 差 1930')
print('='*70)

cur = ora.cursor()
cur.execute('SELECT COUNT(*) FROM RACE.GOODSATTR')
total_ora = cur.fetchone()[0]
cur.execute('SELECT COUNT(DISTINCT GOODSID) FROM RACE.GOODSATTR')
distinct_ora = cur.fetchone()[0]
print(f'Oracle: 总行 {total_ora}, 去重 GOODSID 后 {distinct_ora}, 重复行 {total_ora - distinct_ora}')

cur.execute('''
    SELECT GOODSID, COUNT(*) as cnt
    FROM RACE.GOODSATTR
    GROUP BY GOODSID
    HAVING COUNT(*) > 1
    ORDER BY cnt DESC
    FETCH FIRST 5 ROWS ONLY
''')
dups = cur.fetchall()
print(f'重复 GOODSID: {len(dups)} 组')
for r in dups:
    print(f'  GOODSID={r[0]} 出现 {r[1]} 次')

duck_total = duck.execute('SELECT COUNT(*) FROM raw.GOODSATTR').fetchone()[0]
duck_distinct = duck.execute('SELECT COUNT(DISTINCT GOODSID) FROM raw.GOODSATTR').fetchone()[0]
print(f'DuckDB: 总行 {duck_total}, 去重后 {duck_distinct}')

# 看重复的 GOODSID 内容是否不同
if dups:
    gid = dups[0][0]
    cur.execute(f"SELECT GOODSID, LASTMODIFYTIME, BEACTIVE, ISMEDICARE, MEDCARECODE, SALEP FROM RACE.GOODSATTR WHERE GOODSID = '{gid}'")
    rows = cur.fetchall()
    print(f'\n抽查重复 GOODSID={gid} 的 {len(rows)} 行:')
    for r in rows:
        print(f'  {r}')
    
    duck_rows = duck.execute(f"SELECT GOODSID, LASTMODIFYTIME, BEACTIVE, ISMEDICARE, MEDCARECODE, SALEP FROM raw.GOODSATTR WHERE GOODSID = '{gid}'").fetchall()
    print(f'DuckDB 保留了:')
    for r in duck_rows:
        print(f'  {r}')

cur.close()

print()
print('='*70)
print('3. K_PRN_SENDLOG -- Oracle 19967, DuckDB 19733, 差 234')
print('='*70)

cur = ora.cursor()
cur.execute('SELECT COUNT(*) FROM RACE.K_PRN_SENDLOG')
total_ora = cur.fetchone()[0]
cur.execute('SELECT COUNT(DISTINCT GUID) FROM RACE.K_PRN_SENDLOG')
distinct_ora = cur.fetchone()[0]
print(f'Oracle: 总行 {total_ora}, 去重 GUID 后 {distinct_ora}, 重复行 {total_ora - distinct_ora}')

cur.execute('''
    SELECT GUID, COUNT(*) as cnt
    FROM RACE.K_PRN_SENDLOG
    GROUP BY GUID
    HAVING COUNT(*) > 1
    FETCH FIRST 5 ROWS ONLY
''')
dups = cur.fetchall()
print(f'重复 GUID: {len(dups)} 组')
for r in dups:
    print(f'  GUID={r[0][:30]}... 出现 {r[1]} 次')

# 看重复的行内容是否不同
if dups:
    guid = dups[0][0]
    cur.execute(f"SELECT GUID, CREATETIME, BILLNO, LOGISTICCODE, ISEND FROM RACE.K_PRN_SENDLOG WHERE GUID = '{guid}'")
    rows = cur.fetchall()
    print(f'\n抽查重复 GUID 的 {len(rows)} 行:')
    for r in rows:
        print(f'  GUID={r[0][:20]}... CREATETIME={r[1]} BILLNO={r[2]} LOGISTIC={r[3]} ISEND={r[4]}')

duck_total = duck.execute('SELECT COUNT(*) FROM raw.K_PRN_SENDLOG').fetchone()[0]
duck_distinct = duck.execute('SELECT COUNT(DISTINCT GUID) FROM raw.K_PRN_SENDLOG').fetchone()[0]
print(f'DuckDB: 总行 {duck_total}, 去重后 {duck_distinct}')

cur.close()

ora.close()
duck.close()
