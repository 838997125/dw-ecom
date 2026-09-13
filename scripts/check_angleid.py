import sys, yaml
sys.path.insert(0, ".")
from src.utils.db import resolve_secret
sys.path.insert(0, '.')
from src.utils.db import OracleConn
from pathlib import Path

with open(Path('config/config.yaml')) as f:
    config = yaml.safe_load(f)
o = config['oracle']
pw = resolve_secret(o.get('password',''), 'ORACLE_PASSWORD')
conn = OracleConn(host=o['host'], port=o['port'], service_name=o['service_name'],
                  username=o['username'], password=pw, schema=o.get('schema','RACE'))
cur = conn.conn.cursor()

# 查 Oracle 的列注释
cur.execute("""
    SELECT table_name, column_name, comments
    FROM all_col_comments
    WHERE owner = 'RACE' AND column_name IN ('ANGLEID', 'ANGLECODE', 'ANGLENAME', 'LOCATID', 'ANGLEDATE', 'ANGLEDESC')
    ORDER BY table_name, column_name
""")
rows = cur.fetchall()
print('Oracle 列注释:')
for r in rows:
    print(f'  {r[0]}.{r[1]} -> {r[2] if r[2] else "(无注释)"}')

if not rows:
    print('  (无任何注释)')

# 看几条实际数据
cur.execute("""
    SELECT b.ANGLEID, b.ANGLECODE, b.ANGLENAME, b.BATCHCODE, b.GOODSID,
           a.PLACENUM, a.LOCATID, a.STORMIN, a.STORMAX
    FROM RACE.BATCHCODE b
    LEFT JOIN RACE.ANGLEBALANCE a ON b.ANGLEID = a.ANGLEID
    WHERE b.ANGLEID IS NOT NULL AND ROWNUM <= 5
""")
print()
print('实际数据示例:')
cols = [c[0] for c in cur.description]
print('  ' + ' | '.join(cols))
for r in cur:
    print('  ' + ' | '.join([str(v or '') for v in r]))

# 看 ANGLEBALANCE 的全部字段
cur.execute("""
    SELECT column_name, data_type, data_length
    FROM all_tab_columns
    WHERE owner = 'RACE' AND table_name = 'ANGLEBALANCE'
    ORDER BY column_id
""")
print()
print('ANGLEBALANCE 表结构:')
for r in cur:
    print(f'  {r[0]:20s} {r[1]:15s} {r[2]}')

cur.close()
conn.close()
