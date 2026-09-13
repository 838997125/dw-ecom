"""
DuckDB MCP Server — dw-ecom 数据层统一入口
Provides `query_duckdb`, `get_schema`, `get_kpi` tools via MCP stdio protocol.
"""

import json
import sys
import os
from pathlib import Path

import duckdb

# DB 路径解析优先级：环境变量 DUCKDB_PATH > 项目内 data/duckdb/ecom.duckdb
# server.py 位于 <ROOT>/duckdb-mcp-server/src/server.py，向上三级即项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = str(_PROJECT_ROOT / 'data' / 'duckdb' / 'ecom.duckdb')
DB_PATH = os.environ.get('DUCKDB_PATH', _DEFAULT_DB_PATH)

# ============================================================
# Tool: query_duckdb — execute read-only SQL
# ============================================================

READ_ONLY_ALWAYS = True  # Enforce read-only at connection level

def query_duckdb(db_path: str, sql: str, params: list | None = None) -> str:
    """
    Execute a read-only SQL query against the DuckDB database.
    Returns JSON string: list of rows on success, {"error": "..."} on failure.
    """
    sql_upper = sql.strip().upper()

    # --- Read-only enforcement ---
    forbidden_prefixes = (
        'INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE', 'ALTER',
        'TRUNCATE', 'GRANT', 'REVOKE', 'ATTACH', 'DETACH',
    )
    for prefix in forbidden_prefixes:
        if sql_upper.startswith(prefix) or sql_upper.startswith(f'EXPLAIN {prefix}'):
            return json.dumps({
                'error': f'Write operation rejected: {prefix} statements are not allowed.'
            })

    # Retry on lock conflicts (CDC write lock is transient)
    import time as _time
    last_exc = None
    for attempt in range(6):
        try:
            con = duckdb.connect(db_path, read_only=READ_ONLY_ALWAYS)
            if params:
                result = con.execute(sql, params).fetchall()
            else:
                result = con.execute(sql).fetchall()
            con.close()
            return json.dumps(result, default=str)
        except Exception as e:
            last_exc = e
            if 'lock' in str(e).lower() and attempt < 5:
                _time.sleep(2)
                continue
            try: con.close()
            except: pass
            return json.dumps({'error': str(last_exc)})


# ============================================================
# Tool: get_schema — table structure overview
# ============================================================

def get_schema(db_path: str, schema: str | None = None, table: str | None = None) -> str:
    """
    Return table/column structure.
    - No args: list all schemas with table counts
    - schema only: list all tables in that schema with columns
    - schema + table: list all columns of that table
    Returns JSON string.
    """
    try:
        con = duckdb.connect(db_path, read_only=True)

        if table and schema:
            # Column details for a specific table
            cols = con.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                [schema, table]
            ).fetchall()
            con.close()
            return json.dumps([
                {'column_name': c[0], 'data_type': c[1], 'is_nullable': c[2]}
                for c in cols
            ], default=str)

        if schema:
            # All tables in schema with column counts
            tables = con.execute(
                "SELECT table_name, "
                "(SELECT COUNT(*) FROM information_schema.columns c "
                " WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name) AS col_count "
                "FROM information_schema.tables t "
                "WHERE t.table_schema = ? "
                "ORDER BY t.table_name",
                [schema]
            ).fetchall()
            con.close()
            if not tables:
                return json.dumps([])
            return json.dumps([
                {'schema': schema, 'table': t[0], 'columns': t[1]}
                for t in tables
            ], default=str)

        # Overview: all schemas
        rows = con.execute(
            "SELECT table_schema, COUNT(*) AS table_count "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
            "GROUP BY table_schema "
            "ORDER BY table_schema"
        ).fetchall()
        con.close()
        return json.dumps([
            {'schema': r[0], 'table_count': r[1]}
            for r in rows
        ], default=str)

    except Exception as e:
        return json.dumps({'error': str(e)})


# ============================================================
# Tool: get_kpi — real-time KPI dashboard
# ============================================================

def get_kpi(db_path: str) -> str:
    """
    Return today's KPI dashboard: sales, inventory alerts, overview metrics.
    Returns JSON string with keys: overview, sales, inventory, alerts.
    """
    try:
        if not os.path.exists(db_path):
            return json.dumps({'error': f'Database not found: {db_path}'})

        con = duckdb.connect(db_path, read_only=True)

        today = con.execute("SELECT CURRENT_DATE").fetchone()[0]

        # Overview metrics
        overview = {}
        for metric in ['total_products', 'total_customers', 'total_suppliers', 'total_orders']:
            try:
                if metric == 'total_products':
                    val = con.execute("SELECT COUNT(*) FROM dim.product WHERE status = 'active' OR is_active = 'TRUE'").fetchone()
                elif metric == 'total_customers':
                    val = con.execute("SELECT COUNT(*) FROM dim.customer WHERE is_active = 'TRUE'").fetchone()
                elif metric == 'total_suppliers':
                    val = con.execute("SELECT COUNT(*) FROM dim.supplier WHERE is_active = 'TRUE'").fetchone()
                elif metric == 'total_orders':
                    val = con.execute("SELECT COUNT(*) FROM raw.K_D3OMS_ORDERREFUNDDT").fetchone()
                overview[metric] = val[0] if val else 0
            except Exception:
                overview[metric] = 0

        # Sales KPIs (from agg.daily_sales for today)
        sales = {}
        try:
            sales_row = con.execute(
                "SELECT COALESCE(SUM(order_count),0), COALESCE(SUM(total_qty),0), "
                "COALESCE(SUM(total_amount),0), COALESCE(SUM(total_cost),0), "
                "COALESCE(SUM(gross_profit),0), "
                "CASE WHEN SUM(total_amount) > 0 THEN SUM(gross_profit)/SUM(total_amount) ELSE 0 END "
                "FROM agg.daily_sales WHERE sale_date = ?", [today]
            ).fetchone()
            sales = {
                'order_count': int(sales_row[0]),
                'total_qty': float(sales_row[1]),
                'total_amount': float(sales_row[2]),
                'total_cost': float(sales_row[3]),
                'gross_profit': float(sales_row[4]),
                'gross_margin': float(sales_row[5]),
            }
        except Exception:
            sales = {'order_count': 0, 'total_qty': 0, 'total_amount': 0,
                     'total_cost': 0, 'gross_profit': 0, 'gross_margin': 0}

        # Inventory alerts
        alerts = []
        try:
            alert_rows = con.execute(
                "SELECT alert_date, warehouse_id, product_name, alert_type, "
                "current_qty, threshold_qty, severity "
                "FROM agg.alert_inventory "
                "WHERE alert_date = ? AND status = 'open' "
                "ORDER BY severity DESC, current_qty ASC",
                [today]
            ).fetchall()
            alerts = [
                {
                    'alert_date': str(r[0]), 'warehouse_id': r[1],
                    'product_name': r[2], 'alert_type': r[3],
                    'current_qty': float(r[4]), 'threshold_qty': float(r[5]),
                    'severity': r[6]
                }
                for r in alert_rows
            ]
        except Exception:
            alerts = []

        # Inventory summary
        inventory = {}
        try:
            inv_metrics = con.execute(
                "SELECT "
                "  COUNT(DISTINCT product_id), "
                "  COUNT(DISTINCT warehouse_id) "
                "FROM raw.GOODSDOC"
            ).fetchone()
            inventory = {
                'unique_products': int(inv_metrics[0]) if inv_metrics[0] else 0,
                'active_warehouses': int(inv_metrics[1]) if inv_metrics[1] else 0,
            }
        except Exception:
            inventory = {'unique_products': 0, 'active_warehouses': 0}

        con.close()
        return json.dumps({
            'date': str(today),
            'overview': overview,
            'sales': sales,
            'inventory': inventory,
            'alerts': alerts,
        }, default=str)

    except Exception as e:
        return json.dumps({'error': str(e)})


# ============================================================
# MCP Protocol Handler
# ============================================================

TOOLS_SCHEMA = [
    {
        'name': 'query_duckdb',
        'description': 'Execute a read-only SQL query against the dw-ecom DuckDB database.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'sql': {'type': 'string', 'description': 'SQL SELECT query to execute'},
                'params': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Optional positional parameters for the SQL query'
                },
            },
            'required': ['sql'],
        },
    },
    {
        'name': 'get_schema',
        'description': 'Get table/column structure of the DuckDB database.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'schema': {'type': 'string', 'description': 'Optional: filter by schema name'},
                'table': {'type': 'string', 'description': 'Optional: filter by table name (requires schema)'},
            },
        },
    },
    {
        'name': 'get_kpi',
        'description': 'Get today\'s KPI dashboard: sales, inventory alerts, overview metrics.',
        'inputSchema': {
            'type': 'object',
            'properties': {},
        },
    },
]


SERVER_INFO = {
    'name': 'duckdb-ecom-mcp',
    'version': '1.0.0',
}

PROTOCOL_VERSION = '2025-03-26'


def handle_request(request: dict, db_path: str | None = None) -> dict | None:
    """
    Handle a single MCP JSON-RPC request.
    Returns the JSON-RPC response dict, or None for notifications (no response).
    """
    if db_path is None:
        db_path = DB_PATH

    method = request.get('method', '')
    req_id = request.get('id')

    # --- initialize (MCP handshake) ---
    if method == 'initialize':
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'result': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {
                    'tools': {'listChanged': False},
                },
                'serverInfo': SERVER_INFO,
            },
        }

    # --- notifications/initialized (no response) ---
    if method == 'notifications/initialized':
        return None

    # --- ping ---
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': req_id, 'result': {}}

    # --- tools/list ---
    if method == 'tools/list':
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'result': {'tools': TOOLS_SCHEMA},
        }

    # --- tools/call ---
    if method == 'tools/call':
        params = request.get('params', {})
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})

        try:
            if tool_name == 'query_duckdb':
                sql = arguments['sql']
                qparams = arguments.get('params')
                result_json = query_duckdb(db_path, sql, qparams)
            elif tool_name == 'get_schema':
                result_json = get_schema(
                    db_path,
                    schema=arguments.get('schema'),
                    table=arguments.get('table'),
                )
            elif tool_name == 'get_kpi':
                result_json = get_kpi(db_path)
            else:
                return {
                    'jsonrpc': '2.0', 'id': req_id,
                    'error': {'code': -32601, 'message': f'Unknown tool: {tool_name}'},
                }

            return {
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {
                    'content': [{'type': 'text', 'text': result_json}],
                },
            }
        except KeyError as e:
            return {
                'jsonrpc': '2.0', 'id': req_id,
                'error': {'code': -32602, 'message': f'Missing required parameter: {e}'},
            }
        except Exception as e:
            return {
                'jsonrpc': '2.0', 'id': req_id,
                'error': {'code': -32000, 'message': str(e)},
            }

    # --- Unknown method ---
    return {
        'jsonrpc': '2.0', 'id': req_id,
        'error': {'code': -32601, 'message': f'Unknown method: {method}'},
    }


# ============================================================
# Main stdio loop
# ============================================================

def main():
    """Run MCP server in stdio mode."""
    db_path = os.environ.get('DUCKDB_PATH', DB_PATH)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request, db_path=db_path)
            sys.stdout.write(json.dumps(response) + '\n')
            sys.stdout.flush()
        except json.JSONDecodeError:
            sys.stderr.write(f'Invalid JSON: {line}\n')
            sys.stderr.flush()


if __name__ == '__main__':
    main()
