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

tables = ['GOODSDOC', 'SALEOUTMT', 'SALENOTESMT', 'K_D3OMS_STOCKOUTMT_PLAN']
for t in tables:
    cfg = None
    for c in config.get('cdc',{}).get('incremental',[]):
        if c['table'] == t:
            cfg = c
            break
    if not cfg:
        continue
    col = cfg['cdc_column']
    try:
        cur.execute(f"SELECT DISTINCT {col} FROM RACE.{t} WHERE {col} IS NOT NULL AND ROWNUM <= 3")
        vals = [r[0] for r in cur.fetchall()]
        print(f"{t}.{col}: {[repr(v) for v in vals]}")
    except Exception as e:
        print(f"{t}.{col}: ERROR {e}")

for col in ['SYSDATES', 'DATES', 'ONTIME']:
    try:
        cur.execute(f"SELECT DISTINCT {col} FROM RACE.SALEOUTMT WHERE {col} IS NOT NULL AND ROWNUM <= 1")
        r = cur.fetchone()
        print(f"SALEOUTMT.{col}: {repr(r[0]) if r else 'no rows'}")
    except Exception as e:
        print(f"SALEOUTMT.{col}: ERROR {e}")

cur.close()
conn.close()
