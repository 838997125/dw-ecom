"""
envloader.py - 零依赖 .env 加载器

项目各入口（app.py / CDC / MCP server / scripts）在启动早期调用
``load_env()``，即可从项目根目录的 ``.env`` 读取数据库密码、API Key 等，
无需安装 python-dotenv。

规则：
  - 只设置尚未存在的环境变量（真实环境变量优先于 .env 文件）；
  - 忽略空行、# 注释行；
  - 值两侧的引号会被去掉。
"""
import os
from pathlib import Path


def load_env(path: str | Path | None = None) -> dict:
    if path is None:
        path = Path(__file__).resolve().parent / '.env'
    path = Path(path)
    loaded = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            loaded[key] = val
    return loaded
