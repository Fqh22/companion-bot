"""[orchestration] 笔记页面的元数据：来源 / 最后验证 / 状态 / 保质期 / 项目。

从 learn.py 抽出来（2026-11-16）的原因：wiki 只需要这组纯文件解析函数，
却因为它们在 learn.py 里而不得不模块级 import learn —— 这就是 learn↔wiki 环。
这组函数不依赖 learn 的任何业务（路径常量、数据库、模型调用），只读文件头。

边界：这里只解析/改写文件头，不写 wiki 索引、不碰记忆库。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

def note_project(text: str) -> str:
    """从笔记头部读项目名：`- 项目：xxx`；没有就退回 `- 标签：`。"""
    import re as _re
    m = _re.search(r"(?m)^-\s*项目[：:]\s*(.+)$", text or "")
    if m:
        return m.group(1).strip()[:20] or "通用"
    m = _re.search(r"(?m)^-\s*标签[：:]\s*(.+)$", text or "")
    if m:
        tag = m.group(1).strip()[:20]
        if tag and tag not in ("未分类", "网页学习"):
            return tag
    return "通用"


# ---------- 页面元数据：来源 / 最后验证 / 状态 ----------
# 知识库真正的风险不是「记不住」，是「记错了还当成事实」：
# 笔记全是模型自己写的，它读的时候又分不出「原始输入」和「我上次自己编的」。
# 所以在每页头部落三个字段，索引、kb_read、kb_search 都把它显示出来，
# 让模型知道这段话该按几分可信度用。
META_KEYS = {"来源": "source", "最后验证": "verified", "状态": "status"}

def _meta_field(text: str, name: str) -> str:
    m = re.search(rf"(?m)^-\s*{name}\s*[：:]\s*(.+?)\s*$", text or "")
    return m.group(1).strip() if m else ""

def parse_ts(s: str) -> float:
    """从「2026-11-01 15:55」这类写法里取时间戳；取不到返回 0。"""
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s or "")
    if not m:
        return 0.0
    y, mo, d, hh, mm = (int(x) if x else 0 for x in m.groups())
    try:
        return datetime(y, mo, d, hh, mm).timestamp()
    except Exception:
        return 0.0

def note_meta(text: str) -> dict:
    """解析页面头的字段。老页缺字段时给安全默认：自撰、未验证、保质期未标。"""
    src = _meta_field(text, "来源") or "自撰"
    v = _meta_field(text, "最后验证")
    st = _meta_field(text, "状态").lower()
    ttl_raw = _meta_field(text, "保质期")
    try:
        m = re.search(r"(\d+)", ttl_raw or "")
        # -1 = 文件里根本没有这个字段（交给关键词兜底）；0 = 显式写了「永久/0 天」
        ttl = int(m.group(1)) if m else (-1 if not ttl_raw else 0)
    except Exception:
        ttl = -1
    return {
        "source": src,
        "verified": v,
        "verified_ts": parse_ts(v) if v and v != "未验证" else 0.0,
        "status": "已废弃" if st in ("已废弃", "deprecated", "archived") else "active",
        "stale_days": ttl,
    }

def page_meta(path) -> dict:
    """读一页的元数据；文件头没写来源时**按目录兜底**。

    `learn/raw/` 是对话原文（原始输入，最可信），其余目录都是模型自撰。
    这条兜底很关键：把原文标成「自撰」等于把最可信的材料降级成二手转述。
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {"source": "自撰", "verified": "", "verified_ts": 0.0, "status": "active"}
    meta = note_meta(text)
    if not _meta_field(text, "来源") and "raw" in p.parts:
        meta["source"] = "对话原文"
    return meta

def note_label(meta: dict) -> str:
    """一行短标签，给索引、kb_read、kb_search 用。别写太长，它每轮都在索引里。"""
    if (meta or {}).get("status") == "已废弃":
        return "【已废弃】"
    src = (meta or {}).get("source") or "自撰"
    short = ("自撰" if src.startswith("自撰") else
             "原文" if src.startswith("对话原文") else
             "网页" if src.startswith("网页") else
             "用户" if src.startswith("用户") else src[:4])
    ts = float((meta or {}).get("verified_ts") or 0)
    if not ts:
        return f"【{short}·未验证】"
    days = int((time.time() - ts) / 86400)
    if days > 90:
        return f"【{short}·{days}天未复核】"
    return f"【{short}·已验证】"

def page_label(path) -> str:
    return note_label(page_meta(path))

def set_meta(path, **fields) -> bool:
    """改页面头字段（verified / status / source）。只动头部，正文一个字符不碰。"""
    zh = {v: k for k, v in META_KEYS.items()}
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    # 头部 = 开头连续的那几行「- 键：值」，插入点定在最后一条 bullet 之后
    head_end = 0
    for i, ln in enumerate(lines[:12]):
        if ln.startswith("- "):
            head_end = i + 1
    for zh_name, val in [(k, fields.get(v)) for v, k in zh.items()]:
        if val is None:
            continue
        hit = False
        for i in range(head_end):
            if re.match(rf"^-\s*{zh_name}\s*[：:]", lines[i]):
                lines[i] = f"- {zh_name}：{val}"
                hit = True
                break
        if not hit:
            lines.insert(head_end, f"- {zh_name}：{val}")
            head_end += 1
    try:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False
