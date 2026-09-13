# -*- coding: utf-8 -*-
"""
parse_erp_dict.py - 解析 DBA 提供的《ERP数据库表及字段解释》文本

输出权威字典 erp_authoritative.json: {table: {column: comment}}
作为 apply_schema_comments.py 的最高优先级来源，覆盖之前自动推断/猜测的备注。

文档格式：
    TableName（表中文说明）
    FieldName 中文含义
    FieldName2 中文含义
    （空行分隔表）
"""
import re
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = Path("/Users/<USER>/.openclaw/media/inbound/ERP数据库表及字段解释---<DOC_UUID>.txt")
OUT = ROOT / "scripts" / "erp_authoritative.json"


def parse():
    lines = DOC.read_text(encoding="utf-8").splitlines()
    # 表名行：以大写字母开头、后面跟（中文表名）的行；或 TableName（注释）
    # 字段行：一个标识符 + 空格 + 中文说明
    table_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)（(.+)）\s*$")
    # 字段行：标识符 + 空白 + 说明（说明至少含一个非ASCII字符）
    field_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.+?)\s*$")

    result = {}       # table_upper -> {field_upper: comment}
    table_meta = {}   # table_upper -> 中文表名
    cur = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        m = table_re.match(line)
        if m:
            tname = m.group(1)
            tdesc = m.group(2).strip()
            cur = tname.upper()
            result[cur] = {}
            table_meta[cur] = tdesc
            continue
        f = field_re.match(line)
        if f and cur is not None:
            col = f.group(1)
            comment = f.group(2).strip()
            # 去掉括号内的"主键"等纯技术标注，但保留中文含义
            # 形如 "维度ID （主键）" -> "维度ID"
            comment = re.sub(r"（主键）$", "", comment).strip()
            result[cur][col.upper()] = comment
            continue
        # 其它行（空分隔、分隔符---）忽略

    return table_meta, result


def main():
    table_meta, result = parse()
    # 输出统计
    OUT.write_text(json.dumps({"tables": table_meta, "fields": result},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    total_fields = sum(len(v) for v in result.values())
    print(f"✅ 解析完成: {len(result)} 张表, {total_fields} 个字段 -> {OUT.name}")
    for t, cols in result.items():
        print(f"   {t} ({table_meta[t]}): {len(cols)} 字段")


if __name__ == "__main__":
    main()
