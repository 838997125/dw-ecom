#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零售单据查询 — 钉钉表格自动化专用版（零第三方依赖）

入参（钉钉全局变量注入）:
    fahuo_no   发货快递单号（文本，可为空）
    zhuisuma   退货追溯码（文本，可为空）
    ※ 两个参数不能同时为空

输出: JSON 数组
"""

import sys
import json
import time
import socket
import ssl
import urllib.request
import os
import urllib.error
import urllib.parse

socket.setdefaulttimeout(20)

# ── 配置 ──────────────────────────────────────────────
# 通过环境变量配置，避免把线上地址/密钥写进仓库
API_BASE = os.environ.get("ECOM_MCP_BASE", "http://127.0.0.1:8765")
API_KEY = os.environ.get("MCP_API_KEY", "")
TIMEOUT = 20
MAX_RETRIES = 2

# 忽略 SSL 证书验证（钉钉环境可能有证书链问题）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def http_get(url, headers, timeout=TIMEOUT):
    """发 GET 请求，返回 (status, body_str)。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.status, resp.read().decode("utf-8")


def query_api(keyword):
    """调用 HTTP API 查询，带重试。"""
    url = f"{API_BASE}/api/retail?kw={urllib.parse.quote(keyword.strip())}"
    headers = {
        "X-API-Key": API_KEY,
        "Accept": "application/json",
        "ngrok-skip-browser-warning": "true",
        "User-Agent": "DingTalk-RetailQuery/1.0",
        "Connection": "close",
    }
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            status, body = http_get(url, headers)
            if status == 200:
                data = json.loads(body)
                if "data" in data:
                    return data["data"]
                return data
            else:
                last_err = f"HTTP {status}: {body[:200]}"
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {err_body}"
            if e.code in (502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
    return [{"error": last_err or "查询失败"}]


def _get_input():
    """从钉钉全局变量 / 命令行 / stdin 获取入参。"""
    fahuo_no = ""
    zhuisuma = ""

    # 方式1：钉钉全局变量注入
    try:
        import __main__ as _m
        fahuo_no = str(getattr(_m, "fahuo_no", "") or "").strip()
        zhuisuma = str(getattr(_m, "zhuisuma", "") or "").strip()
    except Exception:
        pass

    # 方式2：命令行参数
    if not fahuo_no and not zhuisuma and len(sys.argv) > 1:
        args = sys.argv[1:]
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("--fahuo_no="):
                fahuo_no = a.split("=", 1)[1]
            elif a.startswith("--zhuisuma="):
                zhuisuma = a.split("=", 1)[1]
            elif a == "--fahuo_no" and i + 1 < len(args):
                fahuo_no = args[i + 1]; i += 1
            elif a == "--zhuisuma" and i + 1 < len(args):
                zhuisuma = args[i + 1]; i += 1
            elif not a.startswith("-") and not fahuo_no:
                fahuo_no = a
            i += 1

    # 方式3：stdin JSON（仅在有管道输入时读取，避免钉钉环境阻塞）
    if not fahuo_no and not zhuisuma:
        raw = ""
        try:
            if not sys.stdin.isatty():
                import os
                if os.name != "nt":
                    import select
                    if select.select([sys.stdin], [], [], 0.5)[0]:
                        raw = sys.stdin.read().strip()
        except Exception:
            raw = ""
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    fahuo_no = str(obj.get("fahuo_no", "") or "").strip()
                    zhuisuma = str(obj.get("zhuisuma", "") or "").strip()
                elif isinstance(obj, (str, int, float)):
                    fahuo_no = str(obj).strip()
            except (json.JSONDecodeError, ValueError):
                fahuo_no = raw

    return fahuo_no, zhuisuma


# ── 顶层执行 ──────────────────────────────────────────
fahuo_no, zhuisuma = _get_input()

if not fahuo_no and not zhuisuma:
    print(json.dumps(
        [{"error": "参数错误：fahuo_no 和 zhuisuma 不能同时为空"}],
        ensure_ascii=False
    ))
else:
    keyword = fahuo_no if fahuo_no else zhuisuma
    result = query_api(keyword)
    print(json.dumps(result, ensure_ascii=False))
