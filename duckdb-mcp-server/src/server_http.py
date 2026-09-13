"""
DuckDB MCP Server — HTTP transport mode
Wraps the same query logic from server.py and exposes it via HTTP,
compatible with MCP streamable-HTTP protocol.

Endpoints:
  POST /mcp    — MCP JSON-RPC endpoint (requires X-API-Key header)
  GET  /health — health check (no auth)

Environment:
  DUCKDB_PATH   — path to .duckdb file (default: ecom.duckdb)
  MCP_API_KEY   — API key for authentication (default: auto-generate)
  MCP_HOST      — bind host (default: 127.0.0.1)
  MCP_PORT      — bind port (default: 8765)
"""

import json
import os
import re
import sys
import secrets
from pathlib import Path

# 启动早期加载项目根 .env（本文件位于 <ROOT>/duckdb-mcp-server/src/）
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from envloader import load_env
    load_env(_ROOT / '.env')
except Exception:
    pass

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn

# Reuse core logic from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from server import handle_request, DB_PATH, TOOLS_SCHEMA, query_duckdb, get_schema, get_kpi

# ============================================================
# Configuration
# ============================================================

DB_PATH_HTTP = os.environ.get('DUCKDB_PATH', DB_PATH)
API_KEY = os.environ.get('MCP_API_KEY', '')
HOST = os.environ.get('MCP_HOST', '127.0.0.1')
PORT = int(os.environ.get('MCP_PORT', '8765'))

# Auto-generate API key if not set — print once on startup
# 密钥文件固定放在项目 duckdb-mcp-server/ 目录下（随项目走，不写死用户目录）
# server_http.py 位于 <ROOT>/duckdb-mcp-server/src/server_http.py
_key_file = Path(__file__).resolve().parents[1] / '.api_key'

if not API_KEY:
    if _key_file.exists():
        API_KEY = _key_file.read_text().strip()
    else:
        API_KEY = secrets.token_urlsafe(32)
        _key_file.parent.mkdir(parents=True, exist_ok=True)
        _key_file.write_text(API_KEY)
        os.chmod(str(_key_file), 0o600)

# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(title="DuckDB MCP HTTP Server", version="1.0.0")


def _verify_api_key(request: Request, x_api_key: str | None = None, authorization: str | None = None) -> None:
    """Raise 401 if API key is missing or wrong.
    Accepts: X-API-Key header, Authorization: Bearer <key>, or ?api_key= query param.
    """
    provided = None
    if x_api_key:
        provided = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    elif request.query_params.get("api_key"):
        provided = request.query_params["api_key"].strip()

    if not provided or not secrets.compare_digest(
        provided.encode('utf-8', errors='replace'),
        API_KEY.encode('utf-8', errors='replace')
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/health")
async def health():
    """No-auth health probe."""
    db_exists = Path(DB_PATH_HTTP).exists()
    db_size_mb = Path(DB_PATH_HTTP).stat().st_size / (1024 * 1024) if db_exists else 0
    return {
        "status": "ok" if db_exists else "warning",
        "db_path": DB_PATH_HTTP,
        "db_exists": db_exists,
        "db_size_mb": round(db_size_mb, 1),
        "tools": [t["name"] for t in TOOLS_SCHEMA],
    }


@app.api_route("/mcp", methods=["GET", "POST"])
async def mcp_endpoint(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """
    MCP JSON-RPC over HTTP.
    Accepts a single JSON-RPC request body and returns the JSON-RPC response.
    Auth via: X-API-Key header, Authorization: Bearer <key>, or ?api_key= query param.
    """
    _verify_api_key(request, x_api_key, authorization)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Support single request or batch
    if isinstance(body, list):
        responses = []
        for req in body:
            resp = handle_request(req, db_path=DB_PATH_HTTP)
            if resp is not None:
                responses.append(resp)
        if responses:
            return JSONResponse(content=responses[0] if len(responses) == 1 else responses)
        return JSONResponse(content=None, status_code=202)
    else:
        response = handle_request(body, db_path=DB_PATH_HTTP)
        if response is None:
            # Notification (no id) — return 202 Accepted
            return JSONResponse(content=None, status_code=202)
        return JSONResponse(content=response)


# ============================================================
# Retail query endpoint (for DingTalk automation / external callers)
# ============================================================

RETAIL_SQL = """
WITH
matched_sale AS (
    SELECT DISTINCT s.billno
    FROM raw.SALEOUTMT s
    LEFT JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o
        ON CAST(s.orderid AS VARCHAR) = CAST(o.erp_order_id AS VARCHAR)
    LEFT JOIN raw.K_D3OMS_ORDERREFUNDMT r
        ON o.refoid = r.refoid
    WHERE s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn')
      AND s.billcode LIKE 'WOT%'
      AND (
          CAST(s.logisticsno AS VARCHAR) = ?
          OR CAST(o.expresscode AS VARCHAR) = ?
          OR r.logisticsorderno = ?
          OR o.refoid = ?
          OR s.b2bcode = ?
      )
    UNION
    SELECT DISTINCT s.billno
    FROM raw.SKWMS_JGM_W w
    JOIN raw.SALEOUTDT d ON w.tran_billno = d.rfbillno AND w.goodsid = d.goodsid
    JOIN raw.SALEOUTMT s ON d.billno = s.billno AND d.entid = s.entid
    WHERE w.imeino = ?
      AND w.tran_type = 'XSCK'
      AND s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn')
      AND s.billcode LIKE 'WOT%'
),
platform_ref AS (
    SELECT billno, MIN(o2.refoid) AS platform_order_no
    FROM raw.SALEOUTMT s2
    LEFT JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o2
        ON CAST(s2.orderid AS VARCHAR) = CAST(o2.erp_order_id AS VARCHAR)
    WHERE s2.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn')
    GROUP BY billno
),
sale_lines AS (
    SELECT
        s.billno, d.billsn AS line_no, d.goodsid, d.rfbillno,
        COALESCE(s.posname, o.posname) AS shop_name,
        pr.platform_order_no,
        g.goodscode, g.goodsname AS goods_title, g.goodsspec AS spec,
        d.num AS quantity, bc.batchcode, bc.valdate AS expiry_date
    FROM matched_sale ms
    JOIN raw.SALEOUTMT s ON ms.billno = s.billno
    JOIN platform_ref pr ON s.billno = pr.billno
    JOIN raw.SALEOUTDT d ON s.billno = d.billno AND s.entid = d.entid
    LEFT JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o
        ON CAST(s.orderid AS VARCHAR) = CAST(o.erp_order_id AS VARCHAR)
    LEFT JOIN LATERAL (
        -- 商品档案：优先同机构(entid)，取不到则回退同 goodsid（跨机构商品编码一致）
        SELECT gg.goodscode, gg.goodsname, gg.goodsspec
        FROM raw.GOODSDOC gg
        WHERE gg.goodsid = d.goodsid
        ORDER BY CASE WHEN gg.entid = d.entid THEN 0 ELSE 1 END
        LIMIT 1
    ) g ON TRUE
    LEFT JOIN raw.BATCHCODE bc ON d.angleid = bc.angleid AND d.goodsid = bc.goodsid AND d.entid = bc.entid
    WHERE s.billcode LIKE 'WOT%'
),
trace_map AS (
    SELECT sl.billno, sl.goodsid,
           STRING_AGG(DISTINCT w.imeino, ',' ORDER BY w.imeino) AS trace_codes
    FROM sale_lines sl
    JOIN raw.SKWMS_JGM_W w ON sl.rfbillno = w.tran_billno
        AND sl.goodsid = w.goodsid AND w.tran_type = 'XSCK'
    WHERE sl.rfbillno IS NOT NULL
    GROUP BY sl.billno, sl.goodsid
),
refund_info AS (
    SELECT DISTINCT o.refoid AS platform_order_no,
        r.logisticsorderno AS return_logistics_no,
        r.logisticscompany AS return_logistics_company
    FROM raw.K_D3OMS_ORDERREFUNDMT r
    JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o ON r.refoid = o.refoid
    WHERE r.logisticsorderno IS NOT NULL AND r.logisticsorderno <> ''
)
SELECT
    sl.shop_name        AS shop,
    sl.platform_order_no AS platform_order_no,
    ri.return_logistics_no,
    ri.return_logistics_company,
    COALESCE(tm.trace_codes, '') AS trace_codes,
    sl.line_no,
    sl.goodscode,
    sl.goods_title,
    sl.spec,
    sl.quantity,
    sl.batchcode,
    sl.expiry_date
FROM sale_lines sl
LEFT JOIN trace_map tm ON sl.billno = tm.billno AND sl.goodsid = tm.goodsid
LEFT JOIN refund_info ri ON sl.platform_order_no = ri.platform_order_no
ORDER BY sl.billno, sl.line_no
"""


@app.get("/api/retail")
async def query_retail(
    request: Request,
    kw: str = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """
    零售单据查询接口（供钉钉表格自动化等外部调用）。

    用法:
        GET /api/retail?kw=<追溯码或单号>
        GET /api/retail?kw=<订单号>
        Header: X-API-Key: <key>  或  Authorization: Bearer <key>

    自动识别快递单号 / 退货物流单号 / 药品追溯码。
    返回 JSON 数组。
    """
    _verify_api_key(request, x_api_key, authorization)

    keyword = (kw or '').strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Missing parameter: kw (快递单号或追溯码)")

    import duckdb, time as _time
    con = None
    for _attempt in range(6):
        try:
            con = duckdb.connect(DB_PATH_HTTP, read_only=True)
            break
        except Exception as e:
            if 'lock' in str(e).lower() and _attempt < 5:
                _time.sleep(2)
                continue
            raise HTTPException(status_code=503, detail=f"Database locked, retry later: {e}")
    try:
        results = _query_retail(con, keyword)
    finally:
        con.close()

    return {"keyword": keyword, "count": len(results), "data": results}


def _query_retail(con, keyword):
    """分步查询零售单据，避免大 SQL 的 OR + JOIN 全表扫描。"""
    kw = keyword.strip()

    # Step 1: 快速定位 billno（分别查各字段，走索引）
    billnos = set()

    # 1a. SALEOUTMT.logisticsno（发货快递单号）
    for r in con.execute(
        "SELECT billno FROM raw.SALEOUTMT "
        "WHERE ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn') "
        "AND billcode LIKE 'WOT%' "
        "AND CAST(logisticsno AS VARCHAR) = ?", [kw]
    ).fetchall():
        billnos.add(r[0])

    # 1b. OMS expresscode（OMS 侧快递单号）
    for r in con.execute(
        "SELECT DISTINCT s.billno FROM raw.SALEOUTMT s "
        "JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o "
        "ON CAST(s.orderid AS VARCHAR) = CAST(o.erp_order_id AS VARCHAR) "
        "WHERE s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn') "
        "AND s.billcode LIKE 'WOT%' AND o.expresscode = ?", [kw]
    ).fetchall():
        billnos.add(r[0])

    # 1c. 退货物流单号
    for r in con.execute(
        "SELECT DISTINCT s.billno FROM raw.K_D3OMS_ORDERREFUNDMT r "
        "JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o ON r.refoid = o.refoid "
        "JOIN raw.SALEOUTMT s ON o.code = s.b2bcode "
        "WHERE s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn') "
        "AND s.billcode LIKE 'WOT%' AND r.logisticsorderno = ?", [kw]
    ).fetchall():
        billnos.add(r[0])

    # 1d. 追溯码 → RFBILLNO → SALEOUTDT → SALEOUTMT
    for r in con.execute(
        "SELECT DISTINCT s.billno FROM raw.SKWMS_JGM_W w "
        "JOIN raw.SALEOUTDT d ON w.tran_billno = d.rfbillno AND w.goodsid = d.goodsid "
        "JOIN raw.SALEOUTMT s ON d.billno = s.billno AND d.entid = s.entid "
        "WHERE w.imeino = ? AND w.tran_type = 'XSCK' "
        "AND s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn') "
        "AND s.billcode LIKE 'WOT%'", [kw]
    ).fetchall():
        billnos.add(r[0])

    # 1e. 原始订单号（平台单号 refoid）——业务最常用的定位方式
    #     经 OMS 出库计划单关联到 ERP 销售出库单
    for r in con.execute(
        "SELECT DISTINCT s.billno FROM raw.K_D3OMS_STOCKOUTMT_PLAN o "
        "JOIN raw.SALEOUTMT s ON o.code = s.b2bcode "
        "WHERE s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn') "
        "AND s.billcode LIKE 'WOT%' AND o.refoid = ?", [kw]
    ).fetchall():
        billnos.add(r[0])

    if not billnos:
        return []

    # Step 2: 查明细行
    placeholders = ','.join('?' * len(billnos))
    rows = con.execute(f"""
        SELECT
            COALESCE(s.posname, o.posname) AS shop,
            o.refoid AS platform_order_no,
            s.b2bcode,
            d.billsn AS line_no,
            d.goodsid,
            g.goodscode,
            g.goodsname,
            g.goodsspec,
            d.num,
            d.rfbillno,
            COALESCE(bc.batchcode, d.batchcode) AS batchcode,
            COALESCE(bc.valdate, d.valdate) AS valdate,
            d.angleid,
            s.logisticsno AS ship_logistics_no
        FROM raw.SALEOUTMT s
        JOIN raw.SALEOUTDT d ON s.billno = d.billno AND s.entid = d.entid
        LEFT JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o
            ON CAST(s.orderid AS VARCHAR) = CAST(o.erp_order_id AS VARCHAR)
        LEFT JOIN LATERAL (
            -- 商品档案：优先同机构(entid)，取不到则回退同 goodsid（跨机构商品编码一致）
            SELECT gg.goodscode, gg.goodsname, gg.goodsspec
            FROM raw.GOODSDOC gg
            WHERE gg.goodsid = d.goodsid
            ORDER BY CASE WHEN gg.entid = d.entid THEN 0 ELSE 1 END
            LIMIT 1
        ) g ON TRUE
        LEFT JOIN raw.BATCHCODE bc ON d.angleid = bc.angleid
            AND d.goodsid = bc.goodsid AND d.entid = bc.entid
        WHERE s.billno IN ({placeholders})
        ORDER BY s.billno, d.billsn
    """, list(billnos)).fetchall()

    if not rows:
        return []

    # Step 3: 批量查追溯码
    rfbillnos = set(r[9] for r in rows if r[9])
    goodsids = set(r[4] for r in rows if r[4])
    trace_map = {}
    if rfbillnos and goodsids:
        rfh = ','.join('?' * len(rfbillnos))
        for r in con.execute(f"""
            SELECT tran_billno, goodsid,
                   STRING_AGG(DISTINCT imeino, ',' ORDER BY imeino)
            FROM raw.SKWMS_JGM_W
            WHERE tran_type = 'XSCK' AND tran_billno IN ({rfh})
            GROUP BY tran_billno, goodsid
        """, list(rfbillnos)).fetchall():
            trace_map[(r[0], r[1])] = r[2]

    # Step 4: 批量查退货物流（一个原始订单号可能多次退货，取最后修改时间最新的一条）
    platform_orders = set(r[1] for r in rows if r[1])
    refund_map = {}
    if platform_orders:
        poh = ','.join('?' * len(platform_orders))
        for r in con.execute(f"""
            SELECT refoid, logisticsorderno, logisticscompany FROM (
                SELECT o.refoid, r.logisticsorderno, r.logisticscompany,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.refoid
                           ORDER BY TRY_CAST(r.lastmodifytime AS TIMESTAMP) DESC NULLS LAST,
                                    r.erp_order_id DESC
                       ) AS rn
                FROM raw.K_D3OMS_ORDERREFUNDMT r
                JOIN raw.K_D3OMS_STOCKOUTMT_PLAN o ON r.refoid = o.refoid
                WHERE r.logisticsorderno IS NOT NULL AND r.logisticsorderno <> ''
                  AND o.refoid IN ({poh})
            ) t WHERE rn = 1
        """, list(platform_orders)).fetchall():
            refund_map[r[0]] = (r[1], r[2])

    # Step 5: 组装结果
    columns = [
        "店铺", "原始订单号", "发货快递单号", "退货物流单号", "退货物流公司", "追溯码",
        "序号", "商品编码", "商品标题", "规格", "数量", "批号", "有效期至"
    ]
    results = []
    for r in rows:
        (shop, platform_no, b2bcode, line_no, goodsid, goodscode,
         goodsname, goodsspec, num, rfbillno, batchcode, valdate, angleid,
         ship_logistics_no) = r

        trace_codes = trace_map.get((rfbillno, goodsid), '') if rfbillno else ''
        refund_no = None
        refund_co = None
        if platform_no and platform_no in refund_map:
            refund_no, refund_co = refund_map[platform_no]

        item = {
            "店铺": shop,
            "原始订单号": platform_no or b2bcode,
            "发货快递单号": ship_logistics_no,
            "退货物流单号": refund_no,
            "退货物流公司": refund_co,
            "追溯码": trace_codes or '',
            "序号": int(line_no) if line_no == int(line_no) else float(line_no),
            "商品编码": goodscode,
            "商品标题": goodsname,
            "规格": goodsspec,
            "数量": float(num) if num and num != int(num) else (int(num) if num else None),
            "批号": batchcode,
            "有效期至": valdate.isoformat() if hasattr(valdate, 'isoformat') else valdate,
        }
        results.append(item)

    return results


# ── 退货业务字典 ──────────────────────────────────────
_REFUND_STATUS_MAP = {
    "REFUNDING": "退款中",
    "WAIT_SELLER_AGREE": "等待商家同意",
    "SUCCESS": "退款成功",
    "SELLER_REFUSE_BUYER": "商家拒绝退货",
    "FAILED": "退款失败",
    "WAIT_BUYER_RETURN_GOODS": "等待买家寄回商品",
    "CLOSED": "退货关闭",
    "WAIT_SELLER_CONFIRM_GOODS": "等待商家确认收货",
    "EXCHANGE_APPLY": "换货申请中",
    "EXCHANGE_WAIT_SEND_GOODS": "换货待发货",
    "EXCHANGE_WAIT_CONFIRM_OUT_GOODS": "换货待确认出库",
    "EXCHANGE_WAIT_IN_GOODS": "换货待买家寄回",
    "EXCHANGE_WAIT_MODIFY": "换货待修改",
    "EXCHANGE_SUCCESS": "换货成功",
    "EXCHANGE_CLOSE": "换货关闭",
}
_REFUND_TYPE_MAP = {
    "REFUND": "仅退款",
    "REFUND_GOODS": "退货退款",
    "RETURN_GOODS_POSTAGE": "退货退款(含运费)",
    "EXCHANGE": "换货",
    "REISSUE_GOODS": "补发",
}
_PLATFORM_MAP = {
    "JD": "京东", "TMALL": "天猫", "PDD": "拼多多", "DY": "抖音",
    "KS": "快手", "ELE": "饿了么", "PAJK": "平安健康",
    "BDJK": "百度健康", "SANGOU_YY": "360好药",
}


def _parse_outer_codes(outer_id):
    """OUTERID 可能是 'PP015629' 或 'CODEA*2+CODEB' 套装，返回编码列表。"""
    if not outer_id:
        return []
    codes = []
    for part in str(outer_id).split('+'):
        p = part.strip()
        p = re.sub(r'\*\d+$', '', p)
        if p:
            codes.append(p)
    return codes


def _query_refund(con, keyword):
    """退货视角查询：返回每个退货申请单（含退款状态/金额/理由 + 退货商品明细行）。

    支持入参：原始订单号 / 退货物流单号 / 平台退货申请单号 / 发货快递单号 / 追溯码。
    一个原始订单号可能有多次退货申请，全部返回并标注是否最新。
    """
    kw = keyword.strip()

    # ── Step 1: 定位原始订单号(refoid)集合 ──
    refoids = set()
    # 1a. 退款表直接字段：原始订单号 / 退货物流单号 / 平台退货申请单号
    for r in con.execute(
        "SELECT DISTINCT refoid FROM raw.K_D3OMS_ORDERREFUNDMT "
        "WHERE refoid = ? OR logisticsorderno = ? OR CAST(erp_order_id AS VARCHAR) = ?",
        [kw, kw, kw]
    ).fetchall():
        if r[0]:
            refoids.add(r[0])
    # 1b. 发货快递单号 / 追溯码 / 退货物流 等复用零售定位逻辑
    try:
        for item in _query_retail(con, kw):
            po = item.get("原始订单号")
            if po:
                refoids.add(str(po))
    except Exception:
        pass

    if not refoids:
        return []

    ref_list = list(refoids)
    ph = ','.join('?' * len(ref_list))

    # ── Step 2: 店铺/平台/发货单号（OMS 出库计划单 + 销售出库单）──
    shop_map = {}
    for r in con.execute(f"""
        SELECT p.refoid,
               MAX(COALESCE(s.posname, p.posname)) AS shop,
               MAX(p.sourceplatformtype)        AS platform,
               MAX(s.logisticsno)                AS ship_no,
               MAX(p.expresscode)                AS oms_express
        FROM raw.K_D3OMS_STOCKOUTMT_PLAN p
        LEFT JOIN raw.SALEOUTMT s ON p.code = s.b2bcode
            AND s.ruleid IN ('36jjy6yj3e6vd5lj','3nroqpch9j6bdggn')
            AND s.billcode LIKE 'WOT%'
        WHERE p.refoid IN ({ph})
        GROUP BY p.refoid
    """, ref_list).fetchall():
        shop_map[r[0]] = {"shop": r[1], "platform": r[2],
                          "ship_no": r[3] or r[4]}

    # ── Step 3: 退货申请主表（同单多次申请，按修改时间排序）──
    apps = con.execute(f"""
        SELECT erp_order_id, refoid, refundphase, logisticsorderno, logisticscompany,
               type, totalfee, refundfee, status, reason, "desc", lastmodifytime
        FROM raw.K_D3OMS_ORDERREFUNDMT
        WHERE refoid IN ({ph}) AND lastmodifytime IS NOT NULL
        ORDER BY refoid, TRY_CAST(lastmodifytime AS TIMESTAMP) DESC, erp_order_id DESC
    """, ref_list).fetchall()

    # 每个 refoid 的最新 erp_order_id
    latest_eid_by_ref = {}
    eids = []
    for a in apps:
        eids.append(str(a[0]))
        if a[1] not in latest_eid_by_ref:
            latest_eid_by_ref[a[1]] = str(a[0])

    # ── Step 4: 退货明细行 ──
    detail_map = {}
    if eids:
        eh = ','.join('?' * len(eids))
        for r in con.execute(f"""
            SELECT erp_order_id, erp_order_sn, outerid, title, num, price, totalfee, refundfee
            FROM raw.K_D3OMS_ORDERREFUNDDT
            WHERE CAST(erp_order_id AS VARCHAR) IN ({eh})
            ORDER BY erp_order_id, erp_order_sn
        """, eids).fetchall():
            detail_map.setdefault(str(r[0]), []).append(r)

    # 商品档案（按 OUTERID 解析出的编码批量匹配）
    all_codes = set()
    for rows in detail_map.values():
        for d in rows:
            all_codes.update(_parse_outer_codes(d[2]))
    goods_map = {}
    if all_codes:
        cl = list(all_codes)
        ch = ','.join('?' * len(cl))
        for r in con.execute(
            f"SELECT goodscode, goodsname, goodsspec FROM raw.GOODSDOC WHERE goodscode IN ({ch})", cl
        ).fetchall():
            goods_map[r[0]] = {"name": r[1], "spec": r[2]}

    # ── Step 5: 组装 ──
    results = []
    for a in apps:
        (eid, refoid, phase, logi_no, logi_co, atype, totalfee,
         refundfee, status, reason, desc, modified) = a
        eid_s = str(eid)
        shop_info = shop_map.get(refoid, {})
        plat = shop_info.get("platform")

        lines = []
        for d in detail_map.get(eid_s, []):
            _, sn, outerid, title, num, price, d_total, d_refund = d
            codes = _parse_outer_codes(outerid)
            first_code = codes[0] if codes else None
            ginfo = goods_map.get(first_code, {}) if first_code else {}
            lines.append({
                "序号": int(sn) if sn is not None and float(sn) == int(sn) else sn,
                "商品编码": first_code or outerid,
                "商品编码/含套装": outerid,
                "商品名称": ginfo.get("name") or title,
                "规格": ginfo.get("spec"),
                "数量": float(num) if num is not None and float(num) != int(num) else (int(num) if num is not None else None),
                "单价": float(price) if price is not None else None,
                "行总额": float(d_total) if d_total is not None else None,
                "行退款额": float(d_refund) if d_refund is not None else None,
            })

        results.append({
            "原始订单号": refoid,
            "平台退货申请单号": eid_s,
            "是否最新": latest_eid_by_ref.get(refoid) == eid_s,
            "店铺": shop_info.get("shop"),
            "平台": _PLATFORM_MAP.get(plat, plat),
            "发货快递单号": shop_info.get("ship_no"),
            "退货物流单号": logi_no,
            "退货物流公司": logi_co,
            "退款阶段": phase,
            "退货类型": _REFUND_TYPE_MAP.get(atype, atype),
            "状态": status,
            "状态解释": _REFUND_STATUS_MAP.get(status, status),
            "整单总额": float(totalfee) if totalfee is not None else None,
            "退款金额": float(refundfee) if refundfee is not None else None,
            "退货理由": reason,
            "备注": desc,
            "最后修改时间": modified.isoformat() if hasattr(modified, 'isoformat') else str(modified) if modified else None,
            "明细": lines,
        })

    # 最新申请排最前
    results.sort(key=lambda x: (x["原始订单号"], 0 if x["是否最新"] else 1, x["最后修改时间"] or ""), reverse=False)
    results.sort(key=lambda x: x["最后修改时间"] or "", reverse=True)
    return results


@app.get("/api/refund")
async def query_refund(
    request: Request,
    kw: str = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """
    退货单据查询接口（退货业务视角，供钉钉表格自动化等外部调用）。

    用法:
        GET /api/refund?kw=<原始订单号>      # 原始订单号
        GET /api/refund?kw=<退货物流单号>          # 退货物流单号
        GET /api/refund?kw=<平台退货申请单号>       # 平台退货申请单号
        Header: X-API-Key: <key>

    自动识别 原始订单号/退货物流单号/退货申请单号/发货快递单号/追溯码，
    返回退款状态、金额、理由及退货商品明细（同单多次退货全部返回，标注是否最新）。
    """
    _verify_api_key(request, x_api_key, authorization)

    keyword = (kw or '').strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="Missing parameter: kw (订单号/物流单号/追溯码)")

    import duckdb, time as _time
    con = None
    for _attempt in range(6):
        try:
            con = duckdb.connect(DB_PATH_HTTP, read_only=True)
            break
        except Exception as e:
            if 'lock' in str(e).lower() and _attempt < 5:
                _time.sleep(2)
                continue
            raise HTTPException(status_code=503, detail=f"Database locked, retry later: {e}")
    try:
        results = _query_refund(con, keyword)
    finally:
        con.close()

    return {"keyword": keyword, "count": len(results), "data": results}


@app.get("/")
async def root():
    return PlainTextResponse(
        "DuckDB MCP HTTP Server\n"
        "  POST /mcp        — MCP JSON-RPC endpoint (X-API-Key required)\n"
        "  GET  /health     — health check\n"
        "  GET  /api/retail — 零售/仓库核对查询 (X-API-Key required)\n"
        "  GET  /api/refund — 退货业务查询 (X-API-Key required)\n",
        media_type="text/plain",
    )


# ============================================================
# Main
# ============================================================

def main():
    print(f"🚀 DuckDB MCP HTTP Server starting...")
    print(f"   DB:   {DB_PATH_HTTP}")
    print(f"   Bind: {HOST}:{PORT}")
    print(f"   Auth: X-API-Key (key length: {len(API_KEY)} chars)")
    print(f"   Key file: {_key_file}")
    print(f"   Health: http://{HOST}:{PORT}/health")
    print(f"   MCP:    POST http://{HOST}:{PORT}/mcp")
    print()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
