"""
app.py - 医药电商数据仓库 Streamlit 仪表盘
启动: streamlit run app.py --server.port 8501 --server.address 0.0.0.0
"""
import sys
import os
import json
import subprocess
import yaml
import time
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st
import duckdb
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

# 启动早期加载项目根 .env（ORACLE_PASSWORD / MYSQL_PASSWORD 等）
try:
    from envloader import load_env
    load_env()
except Exception:
    pass

# ============================================
# 配置
# ============================================
ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "config.yaml"

st.set_page_config(
    page_title="医药电商数据仓库",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


DB_PATH = None

def init_db_path():
    global DB_PATH
    if DB_PATH is None:
        config = get_config()
        DB_PATH = str(ROOT / config["duckdb"]["db_path"])
    return DB_PATH

def get_duckdb_conn(retries: int = 4, retry_wait: float = 5.0):
    """每次创建只读连接，用完即关，避免持锁阻断 CDC 同步。

    CDC 写入时 DuckDB 会短暂持有写锁，只读连接在这一瞬间可能失败，
    所以这里做几次重试，最多等 retries*retry_wait ≈ 20s，对用户透明。
    """
    last_exc = None
    for attempt in range(retries):
        try:
            return duckdb.connect(init_db_path(), read_only=True)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(retry_wait)
    raise last_exc


def run_query(sql: str) -> pd.DataFrame:
    """执行 SQL 返回 DataFrame"""
    conn = get_duckdb_conn()
    try:
        df = conn.execute(sql).fetchdf()
        conn.close()
        return df
    except Exception as e:
        st.error(f"SQL 错误: {e}")
        conn.close()
        return pd.DataFrame()


def run_scalar(sql: str):
    """执行 SQL 返回单个值"""
    conn = get_duckdb_conn()
    try:
        result = conn.execute(sql).fetchone()
        conn.close()
        return result
    except Exception as e:
        conn.close()
        return None


# ============================================
# 侧边栏
# ============================================
st.sidebar.title("💊 医药电商数据仓库")
st.sidebar.caption(f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

page = st.sidebar.radio("导航", [
    "📊 数据概览",
    "📈 销售分析",
    "🔄 CDC 同步状态",
    "📦 退货同步监控",
    "🗄️ 数据表浏览",
    "🔍 SQL 查询台",
    "❤️ 系统健康",
])

# 数据库文件大小
config = get_config()
db_path = ROOT / config["duckdb"]["db_path"]
if db_path.exists():
    db_size_mb = db_path.stat().st_size / 1024 / 1024
    st.sidebar.caption(f"数据库: {db_size_mb:.1f} MB")
else:
    st.sidebar.warning("数据库文件不存在")


# ============================================
# 页面: 数据概览
# ============================================
if page == "📊 数据概览":
    st.title("📊 数据概览")

    # KPI 卡片
    col1, col2, col3, col4 = st.columns(4)

    today_kpi = run_scalar("""
        SELECT COUNT(*), COALESCE(SUM(TAXAMOUNT), 0), COALESCE(SUM(PROFIT), 0), COALESCE(SUM(COSTAMT), 0)
        FROM raw.SALEOUTMT
        WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE
    """)

    if today_kpi:
        orders, revenue, profit, cost = today_kpi
        margin = (profit / revenue * 100) if revenue and revenue > 0 else 0
        col1.metric("今日出库单数", f"{orders:,}")
        col2.metric("今日含税销售额", f"¥{revenue:,.0f}")
        col3.metric("今日毛利", f"¥{profit:,.0f}", f"{margin:.1f}%")
        col4.metric("今日成本", f"¥{cost:,.0f}")

    st.divider()

    # 近 7 天趋势
    col_left, col_right = st.columns([3, 2])

    with col_left:
        st.subheader("近 7 天销售趋势")
        df_trend = run_query("""
            SELECT TRY_CAST(DATES AS DATE) as 日期,
                   COUNT(*) as 单数,
                   COALESCE(SUM(TAXAMOUNT), 0) as 含税销售额,
                   COALESCE(SUM(PROFIT), 0) as 毛利
            FROM raw.SALEOUTMT
            WHERE TRY_CAST(DATES AS DATE) >= CURRENT_DATE - 6
            GROUP BY 1
            ORDER BY 1
        """)
        if not df_trend.empty:
            df_trend["日期"] = pd.to_datetime(df_trend["日期"])
            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=df_trend["日期"], y=df_trend["含税销售额"],
                name="含税销售额", marker_color="#4C9F70",
                text=[f"¥{v:,.0f}" for v in df_trend["含税销售额"]],
                textposition="outside",
            ))
            fig.add_trace(go.Scatter(
                x=df_trend["日期"], y=df_trend["毛利"],
                name="毛利", line=dict(color="#E8743B", width=2),
                mode="lines+markers",
            ))
            fig.update_layout(
                height=350, margin=dict(l=10, r=10, t=10, b=10),
                legend=dict(orientation="h", y=1.1),
                yaxis=dict(title="金额 (¥)"),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("近 7 天暂无数据")

    with col_right:
        st.subheader("今日仓库发货")
        df_wh = run_query("""
            SELECT COALESCE(s.WHNAME, '未知') as 仓库,
                   COUNT(m.BILLNO) as 单数,
                   COALESCE(SUM(d.AMOUNT), 0) as 金额
            FROM raw.SALEOUTMT m
            JOIN raw.SALEOUTDT d ON m.BILLNO = d.BILLNO
            LEFT JOIN raw.STOREHOUSE s ON d.WHID = s.WHID
            WHERE TRY_CAST(m.DATES AS DATE) = CURRENT_DATE
            GROUP BY s.WHNAME
            ORDER BY 金额 DESC
        """)
        if not df_wh.empty:
            fig = px.bar(df_wh, x="仓库", y="金额", text="单数",
                         color="仓库", height=350)
            fig.update_layout(
                showlegend=False, margin=dict(l=10, r=10, t=10, b=10),
                yaxis=dict(title="金额 (¥)"),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("今日暂无发货数据")

    st.divider()

    # 数据量统计
    st.subheader("数据量统计")
    df_tables = run_query("""
        SELECT table_name,
               (SELECT COUNT(*) FROM information_schema.tables t2
                WHERE t2.table_schema = 'raw') as total_raw_tables
        FROM information_schema.tables
        WHERE table_schema = 'raw'
        AND table_name NOT LIKE '\\_cdc%'
        ORDER BY table_name
    """)

    if not df_tables.empty:
        # 逐表取行数
        table_stats = []
        conn = get_duckdb_conn()
        for tn in df_tables["table_name"]:
            try:
                cnt = conn.execute(f'SELECT COUNT(*) FROM raw."{tn}"').fetchone()[0]
                table_stats.append({"表名": tn, "行数": cnt})
            except:
                table_stats.append({"表名": tn, "行数": 0})
        conn.close()

        df_stats = pd.DataFrame(table_stats)
        df_stats["行数"] = df_stats["行数"].apply(lambda x: f"{x:,}")
        st.dataframe(df_stats, use_container_width=True, hide_index=True)


# ============================================
# 页面: 销售分析
# ============================================
elif page == "📈 销售分析":
    st.title("📈 销售分析")

    # 日期选择
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        start_date = st.date_input("开始日期", datetime.now() - timedelta(days=7))
    with col_d2:
        end_date = st.date_input("结束日期", datetime.now())

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # 每日销售
    st.subheader(f"销售趋势 ({start_str} ~ {end_str})")
    df_daily = run_query(f"""
        SELECT TRY_CAST(DATES AS DATE) as 日期,
               COUNT(*) as 单数,
               COALESCE(SUM(TAXAMOUNT), 0) as 含税销售额,
               COALESCE(SUM(PROFIT), 0) as 毛利,
               COALESCE(SUM(COSTAMT), 0) as 成本
        FROM raw.SALEOUTMT
        WHERE TRY_CAST(DATES AS DATE) BETWEEN '{start_str}' AND '{end_str}'
        GROUP BY 1
        ORDER BY 1
    """)

    if not df_daily.empty:
        df_daily["日期"] = pd.to_datetime(df_daily["日期"])
        col_a, col_b = st.columns(2)

        with col_a:
            fig = px.bar(df_daily, x="日期", y=["含税销售额", "毛利", "成本"],
                         barmode="group", height=350)
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10),
                              yaxis=dict(title="金额 (¥)"))
            st.plotly_chart(fig, use_container_width=True)

        with col_b:
            df_daily["毛利率"] = df_daily.apply(
                lambda r: (r["毛利"] / r["含税销售额"] * 100) if r["含税销售额"] > 0 else 0, axis=1)
            fig = px.line(df_daily, x="日期", y="毛利率", height=350,
                          markers=True, line_shape="spline")
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10),
                              yaxis=dict(title="毛利率 %"))
            st.plotly_chart(fig, use_container_width=True)

        # 汇总
        total_orders = df_daily["单数"].sum()
        total_revenue = df_daily["含税销售额"].sum()
        total_profit = df_daily["毛利"].sum()
        overall_margin = (total_profit / total_revenue * 100) if total_revenue > 0 else 0
        st.info(f"区间汇总: {total_orders:,} 单 | 含税销售额 ¥{total_revenue:,.0f} | 毛利 ¥{total_profit:,.0f} | 毛利率 {overall_margin:.1f}%")
    else:
        st.warning("该日期范围暂无数据")

    st.divider()

    # TOP 商品
    st.subheader("畅销商品 TOP 20")
    df_top = run_query(f"""
        SELECT COALESCE(p.product_name, d.GOODSID) as 商品名称,
               COALESCE(p.specification, '') as 规格,
               SUM(d.NUM) as 数量,
               SUM(d.AMOUNT) as 金额
        FROM raw.SALEOUTDT d
        JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
        LEFT JOIN dim.product p ON d.GOODSID = p.product_id
        WHERE TRY_CAST(m.DATES AS DATE) BETWEEN '{start_str}' AND '{end_str}'
        GROUP BY 1, 2
        ORDER BY 金额 DESC
        LIMIT 20
    """)
    if not df_top.empty:
        df_top["金额"] = df_top["金额"].apply(lambda x: f"¥{x:,.0f}")
        df_top["数量"] = df_top["数量"].apply(lambda x: f"{x:,.0f}")
        st.dataframe(df_top, use_container_width=True, hide_index=True)
    else:
        st.info("该日期范围暂无数据")

    st.divider()

    # 按店铺
    st.subheader("按店铺销售")
    df_store = run_query(f"""
        SELECT m.POSNAME as 店铺,
               COUNT(*) as 单数,
               COALESCE(SUM(m.TAXAMOUNT), 0) as 含税销售额,
               COALESCE(SUM(m.PROFIT), 0) as 毛利
        FROM raw.SALEOUTMT m
        WHERE TRY_CAST(m.DATES AS DATE) BETWEEN '{start_str}' AND '{end_str}'
        GROUP BY m.POSNAME
        ORDER BY 含税销售额 DESC
    """)
    if not df_store.empty:
        col_s1, col_s2 = st.columns([3, 2])
        with col_s1:
            st.dataframe(df_store, use_container_width=True, hide_index=True)
        with col_s2:
            fig = px.pie(df_store, values="含税销售额", names="店铺", height=300)
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)


# ============================================
# 页面: CDC 同步状态
# ============================================
elif page == "🔄 CDC 同步状态":
    st.title("🔄 CDC 同步状态")

    # CDC 状态表
    df_cdc = run_query("""
        SELECT table_name as 表名,
               sync_mode as 同步模式,
               status as 状态,
               last_sync_time as 最后同步时间,
               last_cdc_time as 增量位点,
               last_record_count as 本次同步行数,
               error_msg as 错误信息
        FROM raw._cdc_state
        ORDER BY last_sync_time DESC
    """)

    if not df_cdc.empty:
        # 状态标签着色
        def status_color(val):
            if val == "SUCCESS":
                return "background-color: #d4edda"
            elif val == "ERROR":
                return "background-color: #f8d7da"
            elif val == "RUNNING":
                return "background-color: #fff3cd"
            return ""

        st.dataframe(
            df_cdc.style.map(status_color, subset=["状态"]),
            use_container_width=True,
            hide_index=True,
        )

        # 统计
        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        col_s1.metric("同步表总数", len(df_cdc))
        col_s2.metric("成功", len(df_cdc[df_cdc["状态"] == "SUCCESS"]))
        col_s3.metric("错误", len(df_cdc[df_cdc["状态"] == "ERROR"]))
        col_s4.metric("运行中", len(df_cdc[df_cdc["状态"] == "RUNNING"]))

    else:
        st.warning("无 CDC 同步记录")

    # 日志查看
    st.divider()
    st.subheader("CDC 最新日志")
    log_path = ROOT / "logs" / "cdc.log"
    if log_path.exists():
        with open(log_path) as f:
            lines = f.readlines()[-50:]
        lines.reverse()
        st.code("".join(lines), language="text")
    else:
        st.info("日志文件不存在")

# ============================================
# 页面: 退货同步监控
# ============================================
elif page == "📦 退货同步监控":
    st.title("📦 退货订单同步监控")
    st.caption("Oracle ERP -> 钉钉多维表格 | 每 5 分钟自动同步")

    REFUND_SYNC_DIR = ROOT.parent / "refund-sync"
    SYNC_STATUS_FILE = REFUND_SYNC_DIR / "config" / "sync_status.json"
    CHECKPOINT_FILE = REFUND_SYNC_DIR / "config" / "checkpoint.json"
    SYNC_LOG_FILE = REFUND_SYNC_DIR / "logs" / "sync.log"

    # 读取同步状态
    sync_status = None
    if SYNC_STATUS_FILE.exists():
        try:
            with open(SYNC_STATUS_FILE) as f:
                sync_status = json.load(f)
        except Exception:
            pass

    checkpoint = None
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE) as f:
                checkpoint = json.load(f)
        except Exception:
            pass

    # ── 同步状态卡片 ──
    if sync_status:
        status_val = sync_status.get('status', 'unknown')
        details = sync_status.get('details', {})

        status_text = {
            'success': '✅ 成功',
            'error': '❌ 错误',
            'running': '🔄 同步中',
        }.get(status_val, '❓ 未知')

        last_time = sync_status.get('last_sync_time', '-')
        # 兼容两种字段名：sync_refunds.py 成功时写 order_created/detail_created；
        # 旧版本/无数据时写 new_order_records/new_detail_records
        new_order_recs = (details.get('order_created')
                          if details.get('order_created') is not None
                          else details.get('new_order_records',
                                          details.get('new_records', 0)))
        new_detail_recs = (details.get('detail_created')
                           if details.get('detail_created') is not None
                           else details.get('new_detail_records', 0))
        elapsed = f"{details.get('elapsed_seconds', '-')}s"

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.markdown(f"**当前状态**\n{status_text}")
        c2.markdown(f"**最后同步时间**\n{last_time}")
        c3.markdown(f"**新增订单记录**\n{new_order_recs}")
        c4.markdown(f"**新增明细记录**\n{new_detail_recs}")
        c5.markdown(f"**耗时**\n{elapsed}")

        if status_val == 'error':
            error_msg = details.get('error', '未知错误')
            st.error(f"同步错误: {error_msg}")
            if '认证' in error_msg or 'token' in error_msg.lower() or 'authCode' in error_msg:
                st.warning("📌 钉钉认证过期，需要在服务器上运行 `dws auth login --device` 重新授权")
    else:
        st.warning("未找到同步状态文件")

    if checkpoint:
        st.caption(f"增量检查点: {checkpoint.get('last_sync_time', '-')}")

    st.divider()

    # ── 钉钉认证状态 ──
    st.subheader("钉钉认证状态")
    DWS_SCRIPT = os.path.expanduser('~/.local/lib/node_modules/dingtalk-workspace-cli/bin/dws.js')
    NODE_BIN = '/usr/local/bin/node'
    try:
        result = subprocess.run([NODE_BIN, DWS_SCRIPT, 'auth', 'status', '--format', 'json'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0 or not result.stdout.strip():
            st.warning(f"认证状态检查失败 (exit={result.returncode})")
        else:
            auth_data = json.loads(result.stdout)
            if auth_data.get('authenticated'):
                st.success(f"✅ 已认证 | 用户: {auth_data.get('user_name', '-')} | 组织: {auth_data.get('corp_name', '-')}")
                st.caption(f"Refresh Token 过期时间: {auth_data.get('refresh_expires_at', '-')}")
            else:
                st.error(f"❌ 未认证: {auth_data.get('message', '未知原因')}")
                st.code("ssh root@<your-auth-server>\ndws auth login --device\ndws auth export -o /tmp/dws-auth.tar.gz\n# 本机导入:\ndws auth import -i /tmp/dws-auth.tar.gz --force", language='bash')
    except Exception as e:
        st.warning(f"无法检查认证状态: {e}")

    st.divider()

    # ── 同步历史统计 ──
    st.subheader("同步历史统计")
    if SYNC_LOG_FILE.exists():
        try:
            with open(SYNC_LOG_FILE, encoding='utf-8') as f:
                all_lines = f.readlines()

            sync_records = []
            current = {}
            for line in all_lines:
                if '=== 开始同步' in line:
                    # 时间格式: 2026-07-29 19:05:01,234 [INFO] === 开始同步
                    ts = line.split(' [INFO]')[0].strip()
                    # 只取日期时间部分
                    parts = ts.split(',')
                    ts = parts[0]
                    mode = '全量' if 'full=True' in line else '增量'
                    current = {'时间': ts, '模式': mode, 'success': None, 'created_order': 0, 'created_detail': 0}
                elif ('创建' in line or '新建' in line) and ('条新明细记录' in line or '条订单明细记录' in line or '条明细记录' in line):
                    # 注意：明细判断必须在订单前面，因为"条新明细记录"包含"条新记录"
                    try:
                        import re
                        match = re.search(r'(\d+)\s*条', line)
                        if match:
                            current['created_detail'] = int(match.group(1))
                    except:
                        pass
                elif ('创建' in line or '新建' in line) and ('条新订单记录' in line or '条订单主记录' in line or '条新记录' in line or '条订单记录' in line):
                    # 退款同步日志格式: "创建 N 条新记录"
                    try:
                        import re
                        match = re.search(r'(\d+)\s*条', line)
                        if match:
                            current['created_order'] = int(match.group(1))
                    except:
                        pass
                elif '=== 同步完成' in line:
                    # 格式: === 同步完成，耗时 43.5s (主表 新建:2 更新:0, 明细 新建:3) ===
                    import re
                    try:
                        m = re.search(r'耗时\s*([\d.]+s?)', line)
                        current['elapsed'] = m.group(1) if m else '-'
                    except:
                        current['elapsed'] = '-'
                    # 从完成行兜底提取新建数（防止前面的行没匹配到）
                    try:
                        m_o = re.search(r'主表\s*新建[:：]\s*(\d+)', line)
                        m_d = re.search(r'明细\s*新建[:：]\s*(\d+)', line)
                        if m_o and not current.get('created_order'):
                            current['created_order'] = int(m_o.group(1))
                        if m_d and not current.get('created_detail'):
                            current['created_detail'] = int(m_d.group(1))
                    except:
                        pass
                    current['success'] = True
                    sync_records.append(current)
                    current = {}
                elif '=== 同步失败' in line:
                    current['success'] = False
                    sync_records.append(current)
                    current = {}

            if sync_records:
                recent = sync_records[-20:]
                recent.reverse()
                df_sync = pd.DataFrame([
                    {
                        '时间': r.get('时间', ''),
                        '模式': r.get('模式', ''),
                        '状态': '✅成功' if r.get('success') else '❌失败',
                        '订单记录数': r.get('created_order', 0),
                        '明细记录数': r.get('created_detail', 0),
                        '耗时': r.get('elapsed', '-'),
                    }
                    for r in recent
                ])
                st.dataframe(df_sync, use_container_width=True, hide_index=True,
                            column_config={
                                '订单记录数': st.column_config.NumberColumn('订单记录数', alignment='left'),
                                '明细记录数': st.column_config.NumberColumn('明细记录数', alignment='left'),
                            })

                success_count = sum(1 for r in recent if r.get('success'))
                total = len(recent)
                st.metric("最近 20 次同步成功率", f"{success_count}/{total} = {success_count/total*100:.0f}%")
            else:
                st.info("暂无同步历史记录")
        except Exception as e:
            st.warning(f"解析日志失败: {e}")

    st.divider()

    # ── 手动操作 ──
    st.subheader("手动操作")
    col_m1, col_m2 = st.columns(2)

    with col_m1:
        if st.button("🔄 立即增量同步", type="primary"):
            with st.spinner("正在执行增量同步..."):
                result = subprocess.run(
                    ['python3', str(REFUND_SYNC_DIR / 'sync_refunds.py')],
                    capture_output=True, text=True, timeout=300,
                    cwd=str(REFUND_SYNC_DIR),
                    env={**os.environ, 'PATH': os.path.expanduser('~/.local/bin') + ':' + os.environ.get('PATH', '')}
                )
                if result.returncode == 0:
                    st.success("增量同步完成！")
                else:
                    st.error(f"同步失败: {result.stderr[-500:]}")
                st.rerun()

    with col_m2:
        if st.button("♻️ 全量重建", help="删除并重新同步所有数据"):
            confirm = st.checkbox("确认全量重建")
            if confirm:
                with st.spinner("正在执行全量同步（可能需要 2-3 分钟）..."):
                    result = subprocess.run(
                        ['python3', str(REFUND_SYNC_DIR / 'sync_refunds.py'), '--full'],
                        capture_output=True, text=True, timeout=600,
                        cwd=str(REFUND_SYNC_DIR),
                        env={**os.environ, 'PATH': os.path.expanduser('~/.local/bin') + ':' + os.environ.get('PATH', '')}
                    )
                    if result.returncode == 0:
                        st.success("全量同步完成！")
                    else:
                        st.error(f"同步失败: {result.stderr[-500:]}")
                    st.rerun()

    st.divider()

    # ── 最新日志（与 CDC 页面风格一致）──
    st.subheader("最新日志")
    if SYNC_LOG_FILE.exists():
        with open(SYNC_LOG_FILE, encoding='utf-8') as f:
            lines = f.readlines()[-50:]
        lines.reverse()
        st.code("".join(lines), language="text")
    else:
        st.info("日志文件不存在")


# ============================================
# 页面: 数据表浏览
# ============================================
elif page == "🗄️ 数据表浏览":
    st.title("🗄️ 数据表浏览")

    # 获取所有表
    df_all_tables = run_query("""
        SELECT table_schema as schema, table_name as 表名
        FROM information_schema.tables
        WHERE table_schema IN ('raw', 'agg', 'dim')
        ORDER BY table_schema, table_name
    """)

    if not df_all_tables.empty:
        col_t1, col_t2 = st.columns([1, 3])

        with col_t1:
            schema_filter = st.selectbox("Schema", ["全部"] + df_all_tables["schema"].unique().tolist())
            if schema_filter != "全部":
                df_filtered = df_all_tables[df_all_tables["schema"] == schema_filter]
            else:
                df_filtered = df_all_tables
            selected_table = st.selectbox("选择表", df_filtered["表名"].tolist())
            selected_schema = df_filtered[df_filtered["表名"] == selected_table]["schema"].iloc[0]

        with col_t2:
            if selected_table:
                st.subheader(f"{selected_schema}.{selected_table}")

                # 获取行数
                total_count = run_scalar(f'SELECT COUNT(*) FROM {selected_schema}."{selected_table}"')
                st.caption(f"总行数: {total_count[0]:,}" if total_count else "总行数: 0")

                # 获取列信息
                df_cols = run_query(f"""
                    SELECT column_name as 列名, data_type as 类型
                    FROM information_schema.columns
                    WHERE table_schema = '{selected_schema}' AND table_name = '{selected_table}'
                    ORDER BY ordinal_position
                """)
                if not df_cols.empty:
                    st.caption("列: " + " | ".join(df_cols["列名"].tolist()))

                # 预览数据
                limit = st.slider("显示行数", 10, 500, 50, step=10)
                df_preview = run_query(f'''
                    SELECT * FROM {selected_schema}."{selected_table}" LIMIT {limit}
                ''')
                if not df_preview.empty:
                    st.dataframe(df_preview, use_container_width=True)
                else:
                    st.info("该表无数据")


# ============================================
# 页面: SQL 查询台
# ============================================
elif page == "🔍 SQL 查询台":
    st.title("🔍 SQL 工作台")
    st.caption("只读查询 | DuckDB 引擎 | 支持 SELECT / WITH 语句")

    import time
    import streamlit_ace as stace

    def _write_comment(schema, table, column, comment):
        """把中文备注写回 DuckDB（表备注 column=None，字段备注 column=列名）"""
        try:
            conn = duckdb.connect(init_db_path(), read_only=False)
            comment = (comment or "").strip()
            if column:
                if comment:
                    conn.execute(f'COMMENT ON COLUMN {schema}."{table}"."{column}" IS \'{comment}\'')
                else:
                    conn.execute(f'COMMENT ON COLUMN {schema}."{table}"."{column}" IS NULL')
            else:
                if comment:
                    conn.execute(f'COMMENT ON TABLE {schema}."{table}" IS \'{comment}\'')
                else:
                    conn.execute(f'COMMENT ON TABLE {schema}."{table}" IS NULL')
            conn.close()
            st.success(f"✅ 已保存备注: {schema}.{table}" + (f".{column}" if column else ""))
        except Exception as e:
            st.error(f"保存备注失败: {e}")

    # ── 左侧：表结构树 ──
    col_tree, col_main = st.columns([1, 3], gap="small")

    with col_tree:
        st.markdown("#### 📂 表结构")

        # 缓存表结构（避免每次重查）
        @st.cache_data(ttl=300)
        def get_schema_tree():
            conn = get_duckdb_conn()
            try:
                schemas = conn.execute("""
                    SELECT DISTINCT table_schema
                    FROM information_schema.tables
                    WHERE table_schema IN ('raw', 'dim', 'agg')
                    ORDER BY 1
                """).fetchall()

                # 表级备注
                tbl_comm = {}
                for (sc, tn, cm) in conn.execute("""
                    SELECT schema_name, table_name, comment
                    FROM duckdb_tables()
                    WHERE schema_name IN ('raw','dim','agg') AND comment IS NOT NULL
                """).fetchall():
                    tbl_comm.setdefault(sc, {})[tn] = cm

                # 字段级备注
                col_comm = {}
                for (sc, tn, cn, cm) in conn.execute("""
                    SELECT schema_name, table_name, column_name, comment
                    FROM duckdb_columns()
                    WHERE schema_name IN ('raw','dim','agg') AND comment IS NOT NULL
                """).fetchall():
                    col_comm.setdefault(sc, {}).setdefault(tn, {})[cn] = cm

                tree = {}
                for (schema,) in schemas:
                    tables = conn.execute(f"""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = '{schema}'
                        ORDER BY 1
                    """).fetchall()
                    tree[schema] = {}
                    for (table,) in tables:
                        cols = conn.execute(f"""
                            SELECT column_name, data_type
                            FROM information_schema.columns
                            WHERE table_schema = '{schema}' AND table_name = '{table}'
                            ORDER BY ordinal_position
                        """).fetchall()
                        # (列名, 类型, 备注)
                        cc = col_comm.get(schema, {}).get(table, {})
                        tree[schema][table] = {
                            "comment": tbl_comm.get(schema, {}).get(table, ""),
                            "cols": [(c[0], c[1], cc.get(c[0], "")) for c in cols],
                        }
                return tree
            finally:
                conn.close()

        tree_data = get_schema_tree()

        # 搜索过滤
        search_term = st.text_input("🔍 搜索表名/字段/备注", placeholder="输入关键词过滤...",
                                    label_visibility="collapsed")

        # 渲染树形结构：schema -> table -> 字段（层层展开）
        for schema, tables in tree_data.items():
            # 搜索时过滤匹配的表
            if search_term:
                filtered_tables = {}
                for tname, meta in tables.items():
                    cols = meta["cols"]
                    if (search_term.upper() in tname.upper()
                            or search_term.upper() in meta["comment"].upper()
                            or any(search_term.upper() in c[0].upper() or search_term.upper() in c[2].upper() for c in cols)):
                        filtered_tables[tname] = meta
                if not filtered_tables:
                    continue
                tables = filtered_tables

            # 第一层：schema（默认折叠，搜索时展开）
            with st.expander(f"📁 {schema} ({len(tables)} 表)", expanded=bool(search_term)):
                for tname, meta in tables.items():
                    cols = meta["cols"]
                    # 搜索时如果表名/备注匹配就直接展开字段，否则默认折叠
                    table_match = search_term and (search_term.upper() in tname.upper()
                                                   or search_term.upper() in meta["comment"].upper())
                    col_match = search_term and any(search_term.upper() in c[0].upper() or search_term.upper() in c[2].upper() for c in cols)
                    expand_table = bool(search_term) and (table_match or col_match)

                    # 第二层：table（折叠状态，点击展开看字段）
                    tab_title = f"📋 {tname} ({len(cols)} 列)"
                    if meta["comment"]:
                        tab_title += f"  ·  {meta['comment']}"
                    with st.expander(tab_title, expanded=expand_table):
                        # ── SELECT 快捷按钮置顶（放在所有字段最上面）──
                        if st.button("📝 SELECT *",
                                     key=f"btn_{schema}_{tname}",
                                     use_container_width=True,
                                     help=f"插入 SELECT * FROM {schema}.{tname}"):
                            st.session_state["sql_editor"] = f"SELECT * FROM {schema}.{tname} LIMIT 100;"
                            st.session_state["sql_push_version"] += 1
                            st.rerun()

                        # 字段列表（列名 + 类型 + 中文备注）
                        st.caption("──────── 字段 ────────")
                        for col_name, col_type, col_comment in cols:
                            icon = "🔑" if "ID" in col_name.upper() or col_name.upper() == "BILLNO" else "  "
                            note = f"<span style='color:#4C9F70;font-size:0.85em'>{col_comment}</span>" if col_comment else "<span style='color:#bbb;font-size:0.8em'>未添加备注</span>"
                            st.markdown(f"{icon} `{col_name}` · {note} <span style='color:#888;font-size:0.8em'>{col_type}</span>",
                                       unsafe_allow_html=True)

                        # ── 编辑备注（写回 DuckDB）──
                        with st.expander("✏️ 编辑本表备注", expanded=False):
                            edit_key = f"edittbl_{schema}_{tname}"
                            new_tbl_c = st.text_input("表备注", value=meta["comment"], key=edit_key)
                            if st.button("保存表备注", key=f"savetbl_{schema}_{tname}"):
                                _write_comment(schema, tname, None, new_tbl_c)
                                get_schema_tree.clear()
                                st.rerun()

                            # 字段备注下拉编辑
                            sel_col = st.selectbox("选择字段（可修改其中文备注）",
                                                   [c[0] for c in cols],
                                                   key=f"selcol_{schema}_{tname}")
                            cur_c = dict((c[0], c[2]) for c in cols).get(sel_col, "")
                            new_col_c = st.text_input("字段备注", value=cur_c, key=f"editcol_{schema}_{tname}")
                            if st.button("保存字段备注", key=f"savecol_{schema}_{tname}"):
                                _write_comment(schema, tname, sel_col, new_col_c)
                                get_schema_tree.clear()
                                st.rerun()

    with col_main:
        # ── 初始化 session_state ──
        if "sql_editor" not in st.session_state:
            st.session_state["sql_editor"] = "-- 在此输入 SQL，支持 DuckDB 语法\n-- 快捷键: Ctrl+Enter 执行\nSELECT * FROM raw.SALEOUTMT LIMIT 10;"
        if "sql_push_version" not in st.session_state:
            st.session_state["sql_push_version"] = 0
        if "sql_result_df" not in st.session_state:
            st.session_state["sql_result_df"] = None
        if "sql_result_meta" not in st.session_state:
            st.session_state["sql_result_meta"] = None
        if "sql_error" not in st.session_state:
            st.session_state["sql_error"] = None
        if "sql_history" not in st.session_state:
            st.session_state["sql_history"] = []

        def push_sql(sql_text):
            """推入新 SQL 到编辑器，强制组件重挂载"""
            st.session_state["sql_editor"] = sql_text
            st.session_state["sql_push_version"] += 1
            st.rerun()

        # ── 快捷查询模板 ──
        st.markdown("#### ⚡ 快捷模板")
        templates = {
            "今日销售KPI": """SELECT COUNT(*) as 单数,
       SUM(TAXAMOUNT) as 含税销售额,
       SUM(PROFIT) as 毛利,
       SUM(COSTAMT) as 成本
FROM raw.SALEOUTMT
WHERE TRY_CAST(DATES AS DATE) = CURRENT_DATE""",
            "近7天销售趋势": """SELECT TRY_CAST(DATES AS DATE) as 日期,
       COUNT(*) as 单数,
       SUM(TAXAMOUNT) as 含税销售额,
       SUM(PROFIT) as 毛利
FROM raw.SALEOUTMT
WHERE TRY_CAST(DATES AS DATE) >= CURRENT_DATE - 6
GROUP BY 1
ORDER BY 1""",
            "店铺销售排名": """SELECT POSNAME as 店铺,
       COUNT(*) as 单数,
       SUM(TAXAMOUNT) as 含税销售额,
       SUM(PROFIT) as 毛利,
       ROUND(SUM(PROFIT) / NULLIF(SUM(TAXAMOUNT), 0) * 100, 2) as 毛利率
FROM raw.SALEOUTMT
WHERE TRY_CAST(DATES AS DATE) >= CURRENT_DATE - 30
GROUP BY POSNAME
ORDER BY 含税销售额 DESC""",
            "畅销商品TOP20": """SELECT COALESCE(p.product_name, d.GOODSID) as 商品名称,
       COALESCE(p.specification, '') as 规格,
       SUM(d.NUM) as 数量,
       SUM(d.AMOUNT) as 金额
FROM raw.SALEOUTDT d
JOIN raw.SALEOUTMT m ON d.BILLNO = m.BILLNO
LEFT JOIN dim.product p ON d.GOODSID = p.product_id
WHERE TRY_CAST(m.DATES AS DATE) >= CURRENT_DATE - 7
GROUP BY 1, 2
ORDER BY 金额 DESC
LIMIT 20""",
            "今日退款": """SELECT COUNT(*) as 退款单数,
       COALESCE(SUM(REFUNDFEE), 0) as 退款金额
FROM raw.K_D3OMS_ORDERREFUNDMT
WHERE TRY_CAST(LASTMODIFYTIME AS DATE) = CURRENT_DATE""",
            "库存预警": """SELECT a.alert_date,
       a.warehouse_id,
       w.warehouse_name as 仓库名,
       a.product_id,
       p.product_name as 商品名称,
       a.alert_type,
       a.alert_value
FROM agg.alert_inventory a
LEFT JOIN dim.warehouse w ON a.warehouse_id = w.warehouse_id
LEFT JOIN dim.product p ON a.product_id = p.product_id
ORDER BY a.alert_date DESC, a.alert_value DESC""",
            "CDC同步状态": """SELECT table_name as 表名,
       status as 状态,
       last_sync_time as 最后同步时间,
       last_record_count as 本次同步行数,
       error_msg as 错误信息
FROM raw._cdc_state
ORDER BY last_sync_time DESC""",
            "商品资料查询": """SELECT g.GOODSID as 商品ID,
       g.GOODSNAME as 商品名称,
       g.SPEC as 规格,
       g.BRANDID as 品牌ID,
       g.UNIT as 单位,
       g.PRODPLACE as 产地,
       g.MANUFACTURER as 生产企业
FROM raw.GOODSDOC g
WHERE g.GOODSNAME LIKE '%%'  -- 替换为关键词
LIMIT 50""",
        }

        tpl_cols = st.columns(len(templates))
        for i, (name, sql) in enumerate(templates.items()):
            if tpl_cols[i].button(name, use_container_width=True, key=f"tpl_{i}"):
                push_sql(sql)

        st.divider()

        # ── SQL 编辑器（key 带版本号，push 时强制重挂载）──
        st.markdown("#### ✏️ SQL 编辑器")

        editor_value = stace.st_ace(
            value=st.session_state["sql_editor"],
            language="sql",
            theme="tomorrow_night",
            keybinding="vscode",
            font_size=14,
            tab_size=2,
            height=220,
            wrap=True,
            auto_update=True,
            key=f"ace_editor_v{st.session_state['sql_push_version']}",
        )
        # 同步编辑器内容（不触发 rerun，只存值）
        st.session_state["sql_editor"] = editor_value

        # ── 执行按钮区 ──
        col_run, col_limit, col_clear, col_info = st.columns([1, 1, 1, 2])

        with col_run:
            run_clicked = st.button("▶️ 执行查询", type="primary", use_container_width=True)

        with col_limit:
            auto_limit = st.selectbox("自动 LIMIT", [100, 500, 1000, 5000, "不限制"], index=2,
                                      label_visibility="collapsed")

        with col_clear:
            if st.button("🗑️ 清除结果", use_container_width=True):
                st.session_state["sql_result_df"] = None
                st.session_state["sql_result_meta"] = None
                st.session_state["sql_error"] = None
                st.rerun()

        with col_info:
            st.caption("💡 只允许 SELECT / WITH | Ctrl+Enter 执行")

        # ── 执行查询（仅在点击按钮时执行）──
        if run_clicked:
            sql_text = editor_value.strip() if editor_value else ""
            if not sql_text:
                st.session_state["sql_error"] = "请输入 SQL"
                st.session_state["sql_result_df"] = None
                st.session_state["sql_result_meta"] = None
            else:
                sql_upper = sql_text.upper().strip()
                if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
                    st.session_state["sql_error"] = "❌ 只允许 SELECT 或 WITH 查询"
                    st.session_state["sql_result_df"] = None
                    st.session_state["sql_result_meta"] = None
                elif any(kw in sql_upper for kw in ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ", "ATTACH ", "DETACH ", "COPY "]):
                    st.session_state["sql_error"] = "❌ SQL 包含禁止的写操作关键词"
                    st.session_state["sql_result_df"] = None
                    st.session_state["sql_result_meta"] = None
                else:
                    # 自动加 LIMIT
                    if "LIMIT" not in sql_upper and auto_limit != "不限制":
                        sql_text = sql_text.rstrip(";").rstrip() + f" LIMIT {auto_limit}"

                    t0 = time.time()
                    try:
                        df_result = run_query(sql_text)
                        elapsed = time.time() - t0

                        if not df_result.empty:
                            st.session_state["sql_result_df"] = df_result
                            st.session_state["sql_result_meta"] = {
                                "rows": len(df_result),
                                "cols": len(df_result.columns),
                                "elapsed": elapsed,
                            }
                            st.session_state["sql_error"] = None
                        else:
                            st.session_state["sql_result_df"] = None
                            st.session_state["sql_result_meta"] = {"rows": 0, "cols": 0, "elapsed": elapsed}
                            st.session_state["sql_error"] = None

                        # 记录查询历史
                        st.session_state["sql_history"].insert(0, {
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "sql": editor_value[:200],
                        })
                        if len(st.session_state["sql_history"]) > 10:
                            st.session_state["sql_history"] = st.session_state["sql_history"][:10]

                    except Exception as e:
                        elapsed_str = f"{time.time() - t0:.3f}s"
                        error_msg = str(e).replace('`', '')
                        st.session_state["sql_error"] = f"❌ SQL 执行错误 ({elapsed_str}):\n{error_msg}"
                        st.session_state["sql_result_df"] = None
                        st.session_state["sql_result_meta"] = None
            st.rerun()

        # ── 持久化展示结果（不因编辑 SQL 而消失）──
        if st.session_state.get("sql_error"):
            st.error(st.session_state["sql_error"])

        meta = st.session_state.get("sql_result_meta")
        df_result = st.session_state.get("sql_result_df")

        if df_result is not None and meta and meta.get("rows", 0) > 0:
            st.success(f"✅ 查询成功 | {meta['rows']:,} 行 × {meta['cols']} 列 | {meta['elapsed']:.3f}s")
            st.dataframe(df_result, use_container_width=True, height=400)

            col_dl1, col_dl2 = st.columns(2)
            with col_dl1:
                csv = df_result.to_csv(index=False).encode("utf-8-sig")
                st.download_button("📥 下载 CSV", csv,
                                   f"query_result_{datetime.now().strftime('%H%M%S')}.csv",
                                   "text/csv", use_container_width=True)
            with col_dl2:
                import io
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df_result.to_excel(writer, index=False, sheet_name='查询结果')
                st.download_button("📥 下载 Excel", buffer,
                                   f"query_result_{datetime.now().strftime('%H%M%S')}.xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   use_container_width=True)
        elif meta and meta.get("rows", 0) == 0 and not st.session_state.get("sql_error"):
            st.info(f"查询成功，无数据返回 ({meta['elapsed']:.3f}s)")

        # ── 查询历史 ──
        st.divider()
        st.markdown("#### 📜 最近查询历史")
        if st.session_state["sql_history"]:
            for i, h in enumerate(st.session_state["sql_history"]):
                col_h_time, col_h_sql, col_h_use = st.columns([1, 4, 1])
                with col_h_time:
                    st.caption(h["time"])
                with col_h_sql:
                    st.code(h["sql"], language="sql")
                with col_h_use:
                    if st.button("↩️ 复用", key=f"hist_{i}", use_container_width=True):
                        push_sql(h["sql"])
        else:
            st.caption("暂无查询历史")


# ============================================
# 页面: 系统健康
# ============================================
elif page == "❤️ 系统健康":
    st.title("❤️ 系统健康检查")

    # DuckDB 文件
    st.subheader("数据库文件")
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1024 / 1024
        wal_path = str(db_path) + ".wal"
        wal_exists = os.path.exists(wal_path)
        wal_mb = os.path.getsize(wal_path) / 1024 / 1024 if wal_exists else 0

        col_h1, col_h2, col_h3 = st.columns(3)
        col_h1.metric("DuckDB 文件大小", f"{size_mb:.1f} MB")
        col_h2.metric("WAL 文件", f"{wal_mb:.1f} MB" if wal_exists else "无")
        col_h3.metric("路径", db_path.name)

        if size_mb > 10000:
            st.warning("数据库文件较大，建议执行 VACUUM")
        if wal_mb > 500:
            st.warning("WAL 文件较大，可能有未提交事务")
    else:
        st.error("DuckDB 文件不存在")

    st.divider()

    # 数据时效性 - DATES 是日期级，用 CDC 同步时间判断
    st.subheader("数据时效性")
    cdc_status = run_scalar("""
        SELECT last_sync_time, status, last_record_count
        FROM raw._cdc_state
        WHERE table_name = 'SALEOUTMT'
    """)
    if cdc_status and cdc_status[0]:
        last_sync = cdc_status[0]
        if isinstance(last_sync, str):
            last_sync = datetime.fromisoformat(last_sync.replace('Z', ''))
        age_minutes = (datetime.now() - last_sync).total_seconds() / 60
        col_f1, col_f2, col_f3 = st.columns(3)
        col_f1.metric("SALEOUTMT 最后同步", str(last_sync.strftime('%H:%M:%S')))
        col_f2.metric("同步延迟", f"{age_minutes:.0f} 分钟")
        col_f3.metric("同步状态", cdc_status[1] or '-')
        if cdc_status[1] == 'ERROR':
            st.error("CDC 同步出错，请检查日志")
        elif age_minutes > 10:
            st.warning(f"同步延迟 {age_minutes:.0f} 分钟，可能存在锁冲突")
        else:
            st.success("数据时效正常 ✅")
    else:
        st.warning("SALEOUTMT 无同步记录")

    st.divider()

    # 各表行数概览
    st.subheader("各表数据量")
    df_all = run_query("""
        SELECT t.table_name as 表名,
               COALESCE(dt.comment, '') as 中文备注
        FROM information_schema.tables t
        LEFT JOIN duckdb_tables() dt
          ON dt.schema_name = t.table_schema AND dt.table_name = t.table_name
        WHERE t.table_schema = 'raw'
        AND t.table_name NOT LIKE '\\_cdc%'
        ORDER BY t.table_name
    """)
    if not df_all.empty:
        stats = []
        conn = get_duckdb_conn()
        for _, row in df_all.iterrows():
            tn = row["表名"]
            try:
                cnt = conn.execute(f'SELECT COUNT(*) FROM raw."{tn}"').fetchone()[0]
                stats.append({"表名": tn, "中文备注": row["中文备注"] or "", "行数": cnt})
            except:
                stats.append({"表名": tn, "中文备注": row["中文备注"] or "", "行数": 0})
        conn.close()
        df_stats = pd.DataFrame(stats)
        fig = px.bar(df_stats, x="表名", y="行数", height=350)
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10),
                          xaxis=dict(tickangle=-45))
        st.plotly_chart(fig, use_container_width=True)
        # 展示时加千分位，但保留原始数值用于排序
        df_display = df_stats.copy()
        df_display["行数"] = df_display["行数"].apply(lambda x: f"{x:,}")
        st.dataframe(df_display[["表名", "中文备注", "行数"]], use_container_width=True, hide_index=True)

    st.divider()

    # CDC 同步任务状态
    st.subheader("CDC 同步任务")
    try:
        from src.cdc.cdc_daemon import SCHEDULE, TRIGGERS
        import importlib
        import src.cdc.cdc_daemon as _daemon_mod
        importlib.reload(_daemon_mod)
        SCHEDULE = _daemon_mod.SCHEDULE

        cdc_rows = run_query("""
            SELECT table_name, last_cdc_time, last_record_count,
                   last_sync_time, status, sync_mode
            FROM raw._cdc_state
        """)
        cdc_map = {}
        if not cdc_rows.empty:
            for _, r in cdc_rows.iterrows():
                cdc_map[r["table_name"]] = r

        task_rows = []
        for item in SCHEDULE:
            key = item[0]
            interval_s = item[1]
            mode = item[2]
            opts = item[3] if len(item) > 3 else {}
            table = opts.get("table", key)

            # 跳过内部维护任务（vacuum/verify/compact）单独显示
            if key.startswith("__") and key.endswith("__"):
                task_type = "维护"
                disp_name = key.strip("_")
                if mode == "mysql_full":
                    disp_name = "MySQL全量同步"
                    task_type = "MySQL同步"
                elif mode == "compact":
                    disp_name = "周级压缩"
                elif mode == "vacuum":
                    disp_name = "VACUUM"
                elif mode == "verify":
                    disp_name = "全量校验"
            else:
                task_type = "Oracle"
                disp_name = table

            # 调度描述
            if opts.get("at"):
                sched = f"每天 {opts['at']}" if opts.get("weekday") is None else f"周{['一','二','三','四','五','六','日'][opts['weekday']]} {opts['at']}"
            elif interval_s >= 86400:
                sched = f"每 {interval_s // 86400} 天"
            elif interval_s >= 3600:
                sched = f"每 {interval_s // 3600} 小时"
            elif interval_s >= 60:
                sched = f"每 {interval_s // 60} 分钟"
            else:
                sched = f"每 {interval_s} 秒"

            mode_label = {"incremental": "增量", "incremental_id": "增量(ID)", "full": "全量",
                          "mysql_full": "全量", "verify": "校验", "vacuum": "VACUUM",
                          "compact": "压缩", "agg": "聚合"}.get(mode, mode)

            # 从 _cdc_state 拿最后状态
            state = cdc_map.get(table) if table in cdc_map else None
            if state is not None:
                last_sync = state.get("last_sync_time")
                last_cnt = state.get("last_record_count", 0)
                last_status = state.get("status", "-")
                if last_sync is not None:
                    if isinstance(last_sync, str):
                        last_sync_dt = datetime.fromisoformat(last_sync.replace("Z", ""))
                    else:
                        last_sync_dt = last_sync
                    age_min = (datetime.now() - last_sync_dt).total_seconds() / 60
                    if age_min < 60:
                        age_str = f"{age_min:.0f} 分钟前"
                    elif age_min < 1440:
                        age_str = f"{age_min / 60:.1f} 小时前"
                    else:
                        age_str = f"{age_min / 1440:.1f} 天前"
                    sync_str = last_sync_dt.strftime("%m-%d %H:%M")
                else:
                    age_str = "-"
                    sync_str = "-"
            else:
                sync_str = "-"
                age_str = "-"
                last_cnt = 0
                last_status = "-"

            status_icon = {"SUCCESS": "✅", "ERROR": "❌", "-": "⏳"}.get(last_status, "⏳")

            task_rows.append({
                "类型": task_type,
                "任务": disp_name,
                "模式": mode_label,
                "调度": sched,
                "最后同步": sync_str,
                "距今": age_str,
                "行数": last_cnt or 0,
                "状态": f"{status_icon} {last_status}",
            })

        df_tasks = pd.DataFrame(task_rows)
        st.dataframe(df_tasks, use_container_width=True, hide_index=True,
                     column_config={"行数": st.column_config.NumberColumn(format="%d")})
        st.caption(f"共 {len(task_rows)} 个任务（CDC daemon 常驻运行，PID: "
                   f"`{subprocess.run(['pgrep', '-f', 'cdc_daemon'], capture_output=True, text=True).stdout.strip().split(chr(10))[0] or '未运行'}`）")
    except Exception as e:
        st.warning(f"读取 CDC 任务状态失败: {e}")

    st.divider()

    # crontab 状态
    st.subheader("其他定时任务 (crontab)")
    import subprocess
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            lines = [l for l in result.stdout.strip().split("\n")
                     if l and not l.startswith("#") and l.strip()]
            st.code("\n".join(lines), language="crontab")
            st.caption(f"共 {len(lines)} 条定时任务")
        else:
            st.info("无 crontab 任务")
    except Exception as e:
        st.warning(f"读取 crontab 失败: {e}")
