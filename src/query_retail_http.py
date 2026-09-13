#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零售单据查询 — 钉钉表格自动化专用版（零第三方依赖）

通过 HTTP API 查询 DuckDB，自动识别：
  发货快递单号 / 退货物流单号 / 药品追溯码

用法:
    python3 query_retail_http.py <追溯码或单号>
    echo "<追溯码>" | python3 query_retail_http.py
    python3 query_retail_http.py --pretty <订单号>

输出: JSON 数组
字段: 店铺, 原始订单号, 退货物流单号, 退货物流公司, 追溯码,
      序号, 商品编码, 商品标题, 规格, 数量, 批号, 有效期至

Python: 3.10+（仅需标准库 urllib/json，无需 pip install）
"""

import os
import sys
import json
import argparse
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.error import HTTPError, URLError

# ── 配置 ──────────────────────────────────────────────
API_BASE = os.environ.get("ECOM_MCP_BASE", "http://127.0.0.1:8765")
API_KEY = os.environ.get("MCP_API_KEY", "")
# 如果 ngrok 地址变了，改上面这行即可
TIMEOUT = 30  # 秒


def query(keyword: str) -> dict:
    """调用 HTTP API 查询，返回完整响应 dict。"""
    url = f"{API_BASE}/api/retail?kw={quote(keyword.strip())}"
    req = Request(url, headers={
        "X-API-Key": API_KEY,
        "Accept": "application/json",
        # ngrok free tier 拦截页需要这个 header 绕过
        "ngrok-skip-browser-warning": "true",
    })
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {e.code}: {body}"}
    except URLError as e:
        return {"error": f"网络错误: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(
        description="零售单据查询（HTTP API，零依赖）"
    )
    parser.add_argument("keyword", nargs="?", help="快递单号或追溯码")
    parser.add_argument("--pretty", "-p", action="store_true", help="格式化 JSON")
    parser.add_argument("--raw", "-r", action="store_true",
                        help="输出完整响应（含 keyword/count），默认只输出 data 数组")
    args = parser.parse_args()

    keyword = args.keyword
    if not keyword:
        raw = sys.stdin.read().strip()
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    keyword = (obj.get("input") or obj.get("keyword")
                               or obj.get("value") or "")
                elif isinstance(obj, (str, int, float)):
                    keyword = str(obj)
                else:
                    keyword = raw
            except (json.JSONDecodeError, ValueError):
                keyword = raw
        if not keyword:
            parser.print_help()
            sys.exit(1)

    result = query(keyword)

    if args.raw:
        out = result
    else:
        # 默认只输出 data 数组，方便钉钉表格直接解析
        if "data" in result:
            out = result["data"]
        elif "error" in result:
            out = [{"error": result["error"]}]
        else:
            out = result

    indent = 2 if args.pretty else None
    print(json.dumps(out, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
