"""列描述：BIRD 每个库自带的 ``database_description/<表>.csv``。

``frpm``、``cdscode``、``A11`` 这类缩写，不看说明根本猜不出含义；取值说明（"0: N; 1: Y"）
决定了 WHERE 条件该写 0 还是 'N'。baseline 的失败归因显示错误集中在理解而非语法，
这是补知识的第一步（ROADMAP 2.1）。取舍见 DECISIONS D17。
"""

from __future__ import annotations

import csv
import io
import re
from functools import lru_cache
from pathlib import Path

_WS = re.compile(r"\s+")
_EVIDENCE_PREFIX = re.compile(r"^commonsense evidence:\s*", re.IGNORECASE)
_USELESS = {"not useful", "nan", "none", "null"}


def normalize(name: str | None) -> str:
    """表名、列名的匹配键。

    说明文件里的列名常带尾随空格（``School Code ``），PG 的列名渲染时带引号（``"School Code"``），
    大小写也不统一。任何一处对不上，这一列的说明就会静默丢失。
    """
    return _WS.sub(" ", (name or "").strip().strip('"`[]').strip()).lower()


def _clean(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def _read(path: Path) -> str:
    # BIRD 的说明文件大多是带 BOM 的 UTF-8，但有 4 个是 Windows-1252。
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def describe(row: dict[str, str]) -> str:
    """一行说明压成一句话。和列名重复的部分不写，省 token 也少干扰。"""
    original = normalize(row.get("original_column_name"))
    full = _clean(row.get("column_name"))
    desc = _clean(row.get("column_description"))
    values = _EVIDENCE_PREFIX.sub("", _clean(row.get("value_description")))

    parts: list[str] = []
    if full and normalize(full) != original:
        parts.append(full)
    if desc and normalize(desc) not in {original, normalize(full)}:
        parts.append(desc)
    if values and values.lower() not in _USELESS:
        parts.append(f"取值：{values}")
    return " | ".join(parts)


@lru_cache(maxsize=64)
def load_descriptions(desc_dir: Path) -> dict[tuple[str, str], str]:
    """``{(表, 列): 说明}``，键都经过 ``normalize``。没有有用说明的列不收录。

    同一个库的每道题都要用，按目录缓存。
    """
    out: dict[tuple[str, str], str] = {}
    for f in sorted(Path(desc_dir).glob("*.csv")):
        if f.name.startswith("._"):  # macOS 打包留下的元数据文件
            continue
        table = normalize(f.stem)
        for row in csv.DictReader(io.StringIO(_read(f))):
            row = {(k or "").strip(): v for k, v in row.items()}
            text = describe(row)
            if text:
                out[(table, normalize(row.get("original_column_name")))] = text
    return out
