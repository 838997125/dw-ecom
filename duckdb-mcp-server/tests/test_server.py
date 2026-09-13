"""
DuckDB MCP Server — TDD Test Suite
Phase A: RED stage — tests written BEFORE implementation.
These tests MUST FAIL on first run.
"""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# ============================================================
# Test Data Fixtures
# ============================================================

DB_PATH = os.path.expanduser(
    '~/.openclaw/workspace/dw-ecom/data/duckdb/ecom.duckdb'
)


@pytest.fixture
def db_path():
    """Verify the DuckDB database exists and is accessible."""
    assert os.path.exists(DB_PATH), f"DuckDB not found at {DB_PATH}"
    return DB_PATH


# ============================================================
# RED Tests: query_duckdb
# ============================================================

class TestQueryDuckDB:
    """Tests for the `query_duckdb` tool — execute read-only SQL."""

    def test_query_product_count(self, db_path):
        """Should return row count of dim.product as a list of tuples."""
        from server import query_duckdb
        result = query_duckdb(db_path, "SELECT COUNT(*) AS cnt FROM dim.product")
        rows = json.loads(result)
        assert len(rows) == 1
        assert rows[0][0] > 0  # 51,145 products

    def test_query_table_list(self, db_path):
        """Should list all schemas."""
        from server import query_duckdb
        result = query_duckdb(db_path,
            "SELECT DISTINCT table_schema FROM information_schema.tables ORDER BY table_schema")
        rows = json.loads(result)
        schemas = [r[0] for r in rows]
        assert 'dim' in schemas
        assert 'agg' in schemas
        assert 'raw' in schemas

    def test_query_with_params(self, db_path):
        """Should support parameterized queries to prevent injection."""
        from server import query_duckdb
        result = query_duckdb(db_path,
            "SELECT product_name FROM dim.product WHERE product_id = ? LIMIT 1",
            params=['SP00001'])
        rows = json.loads(result)
        assert len(rows) <= 1

    def test_query_invalid_sql_returns_error(self, db_path):
        """Should return error message for invalid SQL, not crash."""
        from server import query_duckdb
        result = query_duckdb(db_path, "SLECT * FROM nonexistent")
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert 'error' in parsed

    def test_query_readonly_enforcement(self, db_path):
        """Should reject write operations (INSERT/UPDATE/DELETE/DROP)."""
        from server import query_duckdb
        for stmt in [
            "INSERT INTO dim.product VALUES ('test', 'test')",
            "UPDATE dim.product SET product_name='x'",
            "DELETE FROM dim.product",
            "DROP TABLE dim.product",
        ]:
            result = query_duckdb(db_path, stmt)
            parsed = json.loads(result)
            assert 'error' in parsed, f"Should reject: {stmt[:30]}..."


# ============================================================
# RED Tests: get_schema
# ============================================================

class TestGetSchema:
    """Tests for the `get_schema` tool — table structure overview."""

    def test_get_all_schemas(self, db_path):
        """Should return schemas and table counts."""
        from server import get_schema
        result = get_schema(db_path)
        data = json.loads(result)
        assert isinstance(data, list)
        # Should include agg, dim, raw
        schema_names = [d['schema'] for d in data if isinstance(d, dict)]
        assert 'dim' in schema_names
        assert 'agg' in schema_names

    def test_get_specific_schema(self, db_path):
        """Should return tables only for the specified schema."""
        from server import get_schema
        result = get_schema(db_path, schema='dim')
        data = json.loads(result)
        assert isinstance(data, list)
        # All entries should belong to 'dim' schema
        for item in data:
            assert item.get('schema') == 'dim'

    def test_schema_includes_columns(self, db_path):
        """Should include column names and types for each table."""
        from server import get_schema
        result = get_schema(db_path, schema='dim', table='product')
        data = json.loads(result)
        assert isinstance(data, list)
        product_cols = [c['column_name'] for c in data]
        assert 'product_id' in product_cols
        assert 'product_name' in product_cols

    def test_nonexistent_schema(self, db_path):
        """Should return empty or error for nonexistent schema."""
        from server import get_schema
        result = get_schema(db_path, schema='nonexistent_xyz')
        data = json.loads(result)
        assert data == [] or (isinstance(data, dict) and 'error' in data)


# ============================================================
# RED Tests: get_kpi
# ============================================================

class TestGetKPI:
    """Tests for the `get_kpi` tool — real-time KPI dashboard."""

    def test_get_kpi_returns_structure(self, db_path):
        """Should return KPI data with expected key metrics."""
        from server import get_kpi
        result = get_kpi(db_path)
        data = json.loads(result)
        assert isinstance(data, dict)
        # Expected top-level KPI categories
        for key in ['overview', 'sales', 'inventory', 'alerts']:
            assert key in data, f"Missing KPI category: {key}"

    def test_get_kpi_sales_metrics(self, db_path):
        """Sales KPIs should include today's revenue, order count, margin."""
        from server import get_kpi
        result = get_kpi(db_path)
        data = json.loads(result)
        sales = data['sales']
        assert 'today_revenue' in sales or 'total_amount' in str(sales)
        assert 'order_count' in sales or 'orders' in str(sales)

    def test_get_kpi_alerts_structure(self, db_path):
        """Inventory alerts should show low-stock items."""
        from server import get_kpi
        result = get_kpi(db_path)
        data = json.loads(result)
        alerts = data['alerts']
        assert isinstance(alerts, (list, dict))

    def test_get_kpi_empty_db_handled(self):
        """Should not crash on an empty/non-existent DB."""
        from server import get_kpi
        result = get_kpi('/tmp/nonexistent.duckdb')
        data = json.loads(result)
        assert 'error' in data or isinstance(data, dict)


# ============================================================
# RED Tests: MCP Protocol Compliance
# ============================================================

class TestMCPProtocol:
    """Tests for the MCP server protocol (stdio JSON-RPC)."""

    def test_tools_list(self):
        """Should return list of available tools."""
        from server import handle_request
        response = handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {}
        })
        tools = response['result']['tools']
        tool_names = [t['name'] for t in tools]
        assert 'query_duckdb' in tool_names
        assert 'get_schema' in tool_names
        assert 'get_kpi' in tool_names

    def test_tool_call_query(self, db_path):
        """Should handle a tools/call for query_duckdb."""
        from server import handle_request
        response = handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "query_duckdb",
                "arguments": {
                    "sql": "SELECT COUNT(*) FROM dim.product"
                }
            }
        }, db_path=db_path)
        assert 'result' in response
        assert 'content' in response['result']

    def test_tool_call_get_kpi(self, db_path):
        """Should handle a tools/call for get_kpi."""
        from server import handle_request
        response = handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_kpi",
                "arguments": {}
            }
        }, db_path=db_path)
        assert 'result' in response
