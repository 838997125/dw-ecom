# DuckDB MCP Server 接入配置

MCP HTTP 服务对外暴露 3 个只读工具，供 AI Agent / 自动化系统查询数仓。

## 端点（默认本地）

- **MCP Endpoint**: `http://<服务器IP或域名>:8765/mcp`
- **健康检查**: `http://127.0.0.1:8765/health`（无需鉴权）
- **协议**: MCP JSON-RPC over HTTP（POST）
- **鉴权**: Header `X-API-Key: <你的密钥>`（也支持 `Authorization: Bearer <key>`
  或 `?api_key=<key>`）

## API Key 从哪来

- 在 `.env` 设置 `MCP_API_KEY=<强随机字符串>`（推荐，公网暴露必须设置）；
- 未设置时，首次启动会自动生成并写入 `duckdb-mcp-server/.api_key`（权限 600）。

## 可用工具

### 1. query_duckdb
执行只读 SQL。
```json
{"sql": "SELECT ...", "params": ["optional"]}
```
- 禁止 INSERT/UPDATE/DELETE/DROP/CREATE/ALTER 等写操作
- 遇到 CDC 写锁会自动重试（最多约 12 秒）

### 2. get_schema
查看数据库结构。
```json
{"schema": "raw", "table": "GOODSDOC"}
```

### 3. get_kpi
获取今日 KPI 仪表盘。

## 数据库概览

- 库文件：`data/duckdb/ecom.duckdb`（由 `scripts/init_db.py` 创建）
- schema：
  - `raw`（44 表）：ERP 原始镜像 — 销售单、采购单、商品资料、客户等
  - `dim`（5 表）：product / customer / supplier / staff / warehouse
  - `agg`（7 表）：daily_sales、c_retail_detail、kpi 等
  - `ymss_ding / ymss_dwd / ymss_dws`（16 表）：可选 MySQL 镜像

## 示例 cURL

```bash
curl -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MCP_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query_duckdb","arguments":{"sql":"SELECT 1"}}}'
```

## Agent MCP 配置示例

```json
{
  "mcpServers": {
    "duckdb-ecom": {
      "transport": "http",
      "url": "http://<服务器IP或域名>:8765/mcp",
      "headers": { "X-API-Key": "<你的密钥>" }
    }
  }
}
```

## 自启服务

- **Linux**：`sudo bash deploy/install_systemd.sh` 注册
  `dw-ecom-mcp-http`（监听 8765）
- **macOS**：`bash deploy/install_launchd.sh`

## 公网暴露（可选）

如需让云端 Agent 访问内网 MCP，可自行用 Cloudflare Tunnel / ngrok /
反向代理 + HTTPS 转发到 `127.0.0.1:8765`。注意：

1. **务必**设置强随机 `MCP_API_KEY` 并使用 HTTPS；
2. 免费 Quick Tunnel 的 URL 在重启后会变化，生产建议用 Named Tunnel / 自有域名固定；
3. 所有查询强制只读；DuckDB 单写多读，CDC 写入时只读会短暂等待。
