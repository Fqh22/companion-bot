"""知识索引（wiki）：把 learn/ 里的页面编成一份可读目录，细节按需打开。

为什么不再每轮硬塞原文：

- 每轮注入 3 块笔记约 550 tok，而且在缓存前缀之后，永远是全价
- 向量对短查询很弱（实测「智谱的」cos 只有 0.44，「R75800H 没独显」0.419 直接被卡掉）
- 页面多了以后，一份带标题和摘要的目录比相似度更好维护，而且**能一眼看出它知道什么**

现在的分工：

| 场景 | 走哪 |
|---|---|
| 每轮注入 | 本文件的 `index_text()`：一页一行，标题 + 30 字摘要 + 项目 |
| 模型要看细节 | 工具 `kb_read(标题)` —— 打开整页（≤2000 字） |
| 索引里没找到 | 工具 `kb_search(query)` —— 退回向量检索（kb.py） |

索引存在 chat.db 的 `wiki_pages` 表，摘要由模型生成一次、文件改了才重算。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from app_core import paths as _paths

from app_core import learn as learn_mod

log = logging.getLogger("qqbot.wiki")

DB = _paths.DATA / "chat.db"
NOTES = _paths.LEARN / "notes"
SKILLS = _paths.LEARN / "skills"

MAX_PAGE_CHARS = 2000      # kb_read 一次最多返回多少字
INDEX_MAX_PAGES = 16       # 注入索引最多几行
# prompt.py 注入知识时 clip 到 900 字，这里留 880 让两边对齐：
# 之前是 700，等于每轮白空 200 字预算，索引在 11 页就贴顶了。
# prompt.py 注入知识时 clip 到 1000，这里留 980 让两边对齐。
# 700→880 是为了让索引多放几页；880→980 是因为加了【可能过时】标记后每页变长，
# 且知识库在长（现在 12 页），提前留出余量。
INDEX_MAX_CHARS = 980      # 索引整段的上限

# 周检阈值
DEAD_DAYS = 90             # 这么久没被 kb_read 打开过 = 死页
STALE_DAYS = 90            # 验证过、但这么久没复核
UNVERIFIED_DAYS = 60       # 从没验证过，文件又放了这么久
LONG_PAGE = 2000           # 超过就被 kb_read 截断，摘要也只反映开头

# ---- 时效（保质期）----
# 统一 90 天对「显卡多少钱」这种知识太宽松了：两周就变的东西，90 天内都算新鲜，
# 结果就是模型拿着旧价格当真。这里按内容类别给不同的保质期，
# 过期页在索引和正文首行都会标出来——不是删掉，是明确告诉它「这可能过时了」。
TTL_TIERS = (
    # 「峰谷价」「缓存费」「计费」这些说法都不含「价格」二字，实测漏判过一页
    # （DeepSeek API 计费那页被判成 180 天）。宁可多认几个词，也别让会变的知识
    # 混进半年档。
    (14, ("价格", "报价", "多少钱", "售价", "行情", "在售", "优惠", "涨价", "降价",
          "打折", "入手价", "计费", "收费", "资费", "单价", "峰谷价", "折扣")),
    (30, ("版本", "发布", "更新", "新功能", "上线", "开源", "刚出", "新版")),
    (180, ("政策", "法规", "条例", "规定", "标准")),
)
DEFAULT_TTL_DAYS = 180
NEW_DAYS = 30          # 这么多天内写的页，索引里保底展示（防马太效应）


def ttl_days(*texts: str) -> int:
    """这条知识多久算旧。按内容里的词判类别，判不出就按半年。"""
    blob = " ".join(t or "" for t in texts)
    for days, words in TTL_TIERS:
        if any(w in blob for w in words):
            return days
    return DEFAULT_TTL_DAYS


def age_days(page: dict) -> float:
    """距上次「确认过」多少天：验证时间优先，没有就按文件改动时间。"""
    base = float(page.get("verified_ts") or page.get("mtime") or 0)
    if not base:
        return 0.0
    return (time.time() - base) / 86400.0


def page_ttl(page: dict) -> int:
    """这页的保质期：**写笔记时模型自己标的优先**，没标才回落到关键词。

    为什么要让模型标：关键词匹配的是字面不是语义。实测 12 页里 3 页被判非默认，
    其中 2 页是误判——「问版本价格能力时先搜」是方法论却因为含「价格」被判 14 天，
    「DeepSeek 开源架构」是概念却因为「开源」被判 30 天。写笔记的那个模型读过全文，
    它给的判断比正则准。

    三态：
      >0  模型给的保质期
      =0  模型显式判为长期有效 → 用一个很大的值，**不要**再回落关键词
      <0  文件里没这个字段 → 回落到关键词
    """
    try:
        explicit = int(page.get("stale_days") if page.get("stale_days") is not None else -1)
    except Exception:
        explicit = -1
    if explicit > 0:
        return explicit
    if explicit == 0:
        return 3650          # 显式「永久」：实际永不判定过期
    return ttl_days(page.get("title"), page.get("summary"))


def is_stale(page: dict) -> tuple[bool, int, int]:
    """返回 (是否过期, 已过天数, 保质期天数)。"""
    d = age_days(page)
    ttl = page_ttl(page)
    return d > ttl, int(d), ttl

SYSTEM = (
    "你在给个人知识库编目录。读这篇笔记，只输出 JSON，不要解释："
    '{"title": "不超过 14 字，能当文件名", '
    '"summary": "不超过 30 字，写清它能解决什么问题或记住什么"}'
    "笔记可能被多次补充，摘要以最新的结论为准，不要照着第一段抄。"
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    c = _conn()
    with c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS wiki_pages ("
            "path TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,"
            "project TEXT NOT NULL DEFAULT '通用', mtime REAL NOT NULL, ts REAL NOT NULL)")
    # 增量补列（老库没有）：来源 / 最后验证 / 状态 / 最后打开时间。
    # 前三个是「知识可信度」的载体；最后打开时间用来算死页（没人读的知识等于没有）。
    have = {r[1] for r in c.execute("PRAGMA table_info(wiki_pages)")}
    for ddl in ("ALTER TABLE wiki_pages ADD COLUMN source TEXT DEFAULT ''",
                "ALTER TABLE wiki_pages ADD COLUMN verified_ts REAL DEFAULT 0",
                "ALTER TABLE wiki_pages ADD COLUMN status TEXT DEFAULT 'active'",
                "ALTER TABLE wiki_pages ADD COLUMN last_open_ts REAL DEFAULT 0",
                # 写笔记时由模型自己标的保质期（天）。0 = 没标，回落到关键词判定。
                "ALTER TABLE wiki_pages ADD COLUMN stale_days INTEGER DEFAULT -1"):
        col = ddl.split(" ADD COLUMN ")[1].split()[0]
        if col in have:
            continue
        try:
            with c:
                c.execute(ddl)
        except Exception:
            pass
    c.close()


# ---------- 读 ----------
def pages() -> list[dict]:
    init()
    c = _conn()
    rows = c.execute(
        "SELECT path, title, summary, project, source, verified_ts, status, "
        "mtime, last_open_ts, stale_days FROM wiki_pages ORDER BY project, ts DESC").fetchall()
    c.close()
    return [{"path": r[0], "title": r[1], "summary": r[2], "project": r[3],
             "source": r[4] or "", "verified_ts": float(r[5] or 0),
             "status": r[6] or "active", "mtime": float(r[7] or 0),
             "last_open_ts": float(r[8] or 0),
             "stale_days": -1 if r[9] is None else int(r[9])}
            for r in rows]


def archive_candidates(pages: list[dict], now: float | None = None,
                       dead_days: float = DEAD_DAYS) -> list[dict]:
    """挑出该自动归档的页：**很久没人读过**，而且要么过期、要么从没验证过。

    两条边界很关键：

    - 只归档「没人读」的页。刚过保质期但一直在用的页**不归档**——过期的知识
      该提示核实（索引里的【可能过时】），不是直接下架。
    - 验证过且还在保质期内的页不动，哪怕暂时冷。

    返回按「最久没碰」排序的列表，由调用方限量处理。
    """
    now = now or time.time()
    out = []
    for p in pages:
        if p.get("status") == "已废弃":
            continue
        base = float(p.get("last_open_ts") or p.get("mtime") or 0)
        if not base or (now - base) / 86400 < dead_days:
            continue
        stale, _d, _t = is_stale(p)
        if not stale and float(p.get("verified_ts") or 0):
            continue
        out.append(p)
    out.sort(key=lambda p: float(p.get("last_open_ts") or p.get("mtime") or 0))
    return out


def archive_stale(max_pages: int = 5, dead_days: float = DEAD_DAYS) -> list[dict]:
    """自动归档：标成「已废弃」。**文件不删**，只是不进目录，kb_search 仍能搜到；
    想恢复就 `/kb_verify 标题`（它会把状态改回 active）。

    每周限量（默认 5 页），避免某天忽然把半个库下架。
    """
    cands = archive_candidates(pages(), dead_days=dead_days)[:max(0, int(max_pages))]
    done: list[dict] = []
    for p in cands:
        try:
            if learn_mod.set_meta(p["path"], status="已废弃"):
                done.append(p)
        except Exception:
            continue
    if done:
        try:
            c = _conn()
            with c:
                for p in done:
                    c.execute("UPDATE wiki_pages SET status='已废弃' WHERE path=?",
                              (p["path"],))
            c.close()
        except Exception:
            pass
    return done


def _titles(items: list[dict], limit: int = 3) -> str:
    names = [f"《{x['title']}》" for x in items[:limit]]
    if len(items) > limit:
        names.append(f"等 {len(items)} 页")
    return "".join(names)


def health() -> dict:
    """知识库体检：只读、不调模型。返回各类问题，供周报和调试用。

    为什么这些指标：目录和页面在语法上永远「看着正常」，退化只能靠这些数字暴露。
    - 死页：从没打开过 / 很久没打开 —— 没人读的知识等于不存在
    - 超期未验证：写进来以后再没人确认过
    - 超长页：超过 kb_read 的截断线，模型永远只看得到前 2000 字
    - 重复：同一个 slug 在不同日期各建了一页（note_write 的名字匹配没兜住）
    - 溢出：页数超过目录容量，多出来的永远进不了每轮上下文
    """
    ps = [p for p in pages() if p["status"] != "已废弃"]
    now = time.time()
    dead, stale, unverified, long_pages = [], [], [], []
    dead_paths: set[str] = set()
    seen: dict[str, list[dict]] = {}
    for p in ps:
        opened = float(p.get("last_open_ts") or 0)
        base = opened or float(p.get("mtime") or 0)
        if base and (now - base) / 86400 > DEAD_DAYS:
            dead.append(p)
            dead_paths.add(p["path"])
        v = float(p.get("verified_ts") or 0)
        if v and (now - v) / 86400 > STALE_DAYS:
            stale.append(p)
        elif not v and (now - float(p.get("mtime") or 0)) / 86400 > UNVERIFIED_DAYS:
            # 已经是死页的就不重复报：同一页在两条里出现只是噪音
            if p["path"] not in dead_paths:
                unverified.append(p)
        try:
            n = len(Path(p["path"]).read_text(encoding="utf-8"))
        except Exception:
            n = 0
        if n > LONG_PAGE:
            long_pages.append({**p, "chars": n})
        slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(p["path"]).stem)
        seen.setdefault(slug, []).append(p)
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    selfmade = sum(1 for p in ps if (p.get("source") or "自撰").startswith("自撰"))
    return {
        "total": len(ps),
        "dead": dead,
        "stale": stale,
        "unverified": unverified,
        "long": long_pages,
        "dup": dup,
        "overflow": max(0, len(ps) - INDEX_MAX_PAGES),
        "selfmade_ratio": (selfmade / len(ps)) if ps else 0.0,
    }


def audit_sample(n: int = 2) -> list[dict]:
    """挑几页请人校对：优先「从没验证过」且「最久没人打开」的。

    排序键让结果稳定轮转：这一页被 /kb_verify 之后 verified_ts>0，就排到后面去，
    下次自然轮到别的页。不随机，是因为随机会让同一页反复出现、另一些永远不被看。
    """
    ps = [p for p in pages() if p["status"] != "已废弃"]
    if not ps:
        return []
    ps.sort(key=lambda p: (float(p.get("verified_ts") or 0) > 0,
                           float(p.get("last_open_ts") or 0)))
    return ps[:n]


def weekly_text(sample: int = 2) -> str:
    """周日知识库周检的正文（纯函数，发送在 scheduler）。没问题也返回抽样。"""
    h = health()
    if not h["total"]:
        return ""
    lines = [f"📚 知识库周检（{h['total']} 页）"]
    issues = []
    if h["dead"]:
        issues.append(f"死页 {len(h['dead'])}：{_titles(h['dead'])}")
    if h["stale"]:
        issues.append(f"超 {STALE_DAYS} 天没复核 {len(h['stale'])}：{_titles(h['stale'])}")
    if h["unverified"]:
        issues.append(f"从没验过又放了 {UNVERIFIED_DAYS} 天以上 "
                      f"{len(h['unverified'])}：{_titles(h['unverified'])}")
    if h["long"]:
        issues.append(f"超过 {LONG_PAGE} 字（只读得到前 {MAX_PAGE_CHARS} 字）"
                      f"{len(h['long'])}：{_titles(h['long'])}")
    if h["dup"]:
        issues.append("疑似重复：" + "；".join(f"{k}×{len(v)}" for k, v in h["dup"].items()))
    if h["overflow"]:
        issues.append(f"目录只放得下 {INDEX_MAX_PAGES} 页，还有 {h['overflow']} 页进不了索引")
    # 页面太少时「自撰占比 100%」永远成立，只是噪音，凑够 5 页再报
    if h["selfmade_ratio"] >= 0.9 and h["total"] >= 5:
        issues.append(f"自撰占比 {h['selfmade_ratio'] * 100:.0f}%：知识几乎全是自己写的，"
                      "建议多把原始材料放进 learn/raw/")
    if issues:
        lines += [""] + [f"- {x}" for x in issues]
    else:
        lines.append("没发现异常。")
    picks = audit_sample(sample)
    if picks:
        lines += ["", "本周抽这两页校对（还成立就回第一条命令）："]
        for i, p in enumerate(picks, 1):
            lines.append(f"{i}. 《{p['title']}》{(p['summary'] or '')[:18]}…")
            lines.append(f"   → /kb_verify {p['title']}　/kb_deprecate {p['title']}")
    return "\n".join(lines)


def index_text(max_pages: int = INDEX_MAX_PAGES, max_chars: int = INDEX_MAX_CHARS) -> str:
    """给 prompt 用的目录。空索引返回空串，调用方会退回向量检索。

    每行带可信度标签（【自撰·未验证】等）：模型据此决定是当事实讲还是先核实。
    已废弃的页不进目录——它们只留在磁盘和向量库里，供 kb_search 兜底时看见。

    **排序即优先级**：目录会满，满了就截断。按「最近打开 / 最近改动」排序，
    截掉的才是常年没人碰的页；按固定顺序截断，越新的页越容易被挤掉——正好反了。
    """
    ps = [p for p in pages() if p["status"] != "已废弃"]
    if not ps:
        return ""

    def heat(p: dict) -> float:
        """一页有多「热」：被打开过就算打开时间，否则算文件改动时间。"""
        return max(float(p.get("last_open_ts") or 0), float(p.get("mtime") or 0))

    def rank(p: dict) -> tuple:
        """排序键：新写的页保底靠前，其余按热度。

        纯按热度排会有马太效应——不展示 → 没人读 → 继续不展示 → 永远冷。
        刚写进来的知识几乎一定相关，给它一个 30 天的免死期，之后就靠热度说话。
        """
        mt = float(p.get("mtime") or 0)
        is_new = 1 if mt and (time.time() - mt) / 86400 <= NEW_DAYS else 0
        return (is_new, heat(p))

    ps.sort(key=rank, reverse=True)
    # 先按项目分组：条目多了以后，一个平铺列表看不出结构，也分不清哪个项目有哪些沉淀。
    # 组内已按热度降序；组间用各自最热那页排序，截断才会落在最久没动的项目上。
    groups: dict[str, list[dict]] = {}
    for p in ps:
        groups.setdefault(p["project"], []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: rank(kv[1][0]), reverse=True)

    lines, used, shown = [], 0, 0
    for proj, items in ordered:
        head = f"{proj}："
        if used + len(head) > max_chars:
            break
        lines.append(head)
        used += len(head)
        for it in items:
            if shown >= max_pages:
                break
            label = learn_mod.note_label({"source": it["source"],
                                          "verified_ts": it["verified_ts"],
                                          "status": it["status"]})
            stale, d, _ttl = is_stale(it)
            mark = f"【可能过时{d}天】" if stale else ""
            line = f"  - {it['title']}{label}{mark}：{it['summary']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
            shown += 1
    if len(ps) > shown:
        lines.append(f"（还有 {len(ps) - shown} 页放不下，用 kb_search 找）")
    return "\n".join(lines)


def resolve(key: str) -> str:
    """标题 / 文件名 / 关键词 → 文件路径；找不到返回空串。

    文件名比对时**去掉 `YYYY-MM-DD-` 前缀**：笔记落盘叫 `2026-11-01-跨层探针页.md`，
    而模型给索引页起的中文标题可能完全不同（比如「探针页」）。用户和模型
    多半会拿「跨层探针页」来读，不带日期前缀，所以两种都要认。
    """
    k = (key or "").strip()
    if not k:
        return ""
    init()
    c = _conn()
    rows = c.execute("SELECT path, title FROM wiki_pages").fetchall()
    c.close()
    for path, title in rows:
        stem = Path(path).stem
        plain = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
        if k in (title, stem, plain, path) or (k and k in title) or (k and k in plain):
            return path
    return ""


def read(key: str) -> str:
    """按标题 / 文件名 / 路径打开一页。找不到时给出近似标题提示。"""
    k = (key or "").strip()
    if not k:
        return "要给一个标题"
    hit = resolve(k)
    if not hit:
        c = _conn()
        rows = c.execute("SELECT path, title FROM wiki_pages").fetchall()
        c.close()
        cand = [t for _, t in rows if k[:4] and k[:4] in t][:3]
        return ("没找到这一页" + ("；相近的有：" + "、".join(cand) if cand else
                                 "，可以先用 kb_search 搜一下"))
    try:
        text = Path(hit).read_text(encoding="utf-8").strip()
    except Exception:
        return "这页读不出来（文件可能被移走了）"
    # 打开时就报出处：模型得知道这段是它自己写的、还是有人给过的原始材料
    meta = learn_mod.page_meta(hit)
    label = learn_mod.note_label(meta)
    verified = meta.get("verified") or "未验证"
    head = f"[{label} 来源：{meta.get('source') or '自撰'}｜最后验证：{verified}｜{Path(hit).name}]"
    # 打开时就报时效：过期的知识不是不能用，但得让它先想着核实
    fresh = ""
    try:
        mt = Path(hit).stat().st_mtime
        stale, d, ttl = is_stale({"title": k, "summary": text[:200], "mtime": mt})
        if stale:
            fresh = (f"[时效提醒] 这页 {d} 天没更新，按类别保质期 {ttl} 天算可能已过时；"
                     "涉及价格/版本/新闻这类会变的信息，先 web_search 核实再当结论说。\n")
    except Exception:
        pass
    touch_open(hit)
    return (head + "\n" + fresh + text)[:MAX_PAGE_CHARS + 80]


def touch_open(path) -> None:
    """记一笔「这页被打开过」。用来算死页：没人读的知识等于不存在。"""
    try:
        c = _conn()
        with c:
            c.execute("UPDATE wiki_pages SET last_open_ts=? WHERE path=?",
                      (time.time(), str(path)))
        c.close()
    except Exception:
        pass


def upsert_now(path) -> None:
    """写完笔记立刻在索引里占个位（写穿）。

    原来 note_write 只落文件，索引要等下一小时 refresh 才更新——模型刚写完的
    知识，在同一轮或下一轮里 kb_read 打开会说「没找到这一页」。知识写了却读不到，
    是最难察觉的那种断层。

    这里先插一行「临时页」：标题取文件里的 `# 标题`（没有就用文件名去掉日期前缀），
    摘要取正文前 40 字，`mtime=0` 让下一次 refresh 认为它「没生成过摘要」而重新生成。
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return
    if not text:
        return
    m = re.search(r"(?m)^#\s+(.+)$", text)
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", p.stem)
    title = (m.group(1).strip() if m else stem)[:14]
    body = re.sub(r"(?m)^-\s*.+$", "", text).strip()
    summary = (body or text).replace("\n", " ")[:40]
    meta = learn_mod.page_meta(path)
    project = learn_mod.note_project(text)
    init()
    c = _conn()
    with c:
        c.execute(
            "INSERT OR REPLACE INTO wiki_pages"
            "(path, title, summary, project, mtime, ts, source, verified_ts, status,"
            " stale_days) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(p), title, summary, project[:20], 0.0, time.time(),
             (meta.get("source") or "自撰")[:40], float(meta.get("verified_ts") or 0),
             meta.get("status") or "active", int(meta.get("stale_days", -1))))
    c.close()


# ---------- 写 ----------
def set_verified(path, verified_ts: float, status: str = "active") -> None:
    """把「已校验」同步进索引，不重算摘要。

    改文件头会刷新 mtime，但内容没变；走 refresh() 会白烧一次模型生成摘要，
    所以这里只 UPDATE 那两个字段。索引最多滞后到下一次整点刷新。
    """
    init()
    c = _conn()
    with c:
        c.execute("UPDATE wiki_pages SET verified_ts=?, status=? WHERE path=?",
                  (float(verified_ts), status, str(path)))
    c.close()


def _parse(raw: str) -> tuple[str, str]:
    t = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        d = json.loads(t)
    except Exception:
        return "", ""
    return str(d.get("title") or "").strip(), str(d.get("summary") or "").strip()


def _digest_input(text: str, limit: int = 1500) -> str:
    """喂给摘要模型的正文：长页取「开头 + 结尾」。

    原来只发 `text[:1500]`，而 `note_write` 的补充全写在**页尾**——
    页一超过 1500 字，之后的更新永远进不了目录，目录和内容就这么悄悄脱节。
    取尾部能让摘要看见最新那次补充（尾部通常就是最近的几段）。
    """
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    head = limit * 2 // 3
    tail = limit - head
    return t[:head] + "\n……（中间省略）……\n" + t[-tail:]


def _fallback_title(path: Path, text: str) -> str:
    m = re.search(r"(?m)^#\s+(.+)$", text or "")
    return (m.group(1).strip() if m else path.stem)[:14]


async def refresh(core, max_files: int = 40) -> int:
    """给新增/改过的文件生成标题和摘要。文件没变就跳过，不会重复烧模型。"""
    init()
    candidates = []
    for d in (NOTES, SKILLS):
        if not d.exists():
            continue
        for p in d.glob("*.md"):
            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue
            candidates.append((str(p), mtime))
    c = _conn()
    old = {r[0]: r[1] for r in c.execute("SELECT path, mtime FROM wiki_pages")}
    # 文件被删了，索引不能留死行：否则目录里一直挂着一个打不开的页
    # （kb_read 会说「这页读不出来」，而 kb_chunks 早就清掉了）。
    live = {path for path, _ in candidates}
    stale = [p for p in old if p not in live]
    if stale:
        with c:
            c.executemany("DELETE FROM wiki_pages WHERE path=?", [(p,) for p in stale])
        for p in stale:
            old.pop(p, None)
        log.info("知识目录清掉 %d 个已删除的页", len(stale))
    c.close()

    done = 0
    for path, mtime in candidates[:max_files]:
        if old.get(path, 0) >= mtime:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not text:
            continue
        title = summary = ""
        try:
            raw = await core.complete_summary(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": _digest_input(text)}], max_tokens=120)
            title, summary = _parse(raw)
        except Exception:
            pass
        title = title or _fallback_title(Path(path), text)
        summary = summary or text.replace("\n", " ")[:30]
        project = learn_mod.note_project(text)
        # 来源/验证/状态以文件头为准（没有就按目录兜底）；/kb_verify 改的是文件
        meta = learn_mod.page_meta(path)
        c = _conn()
        with c:
            c.execute(
                "INSERT OR REPLACE INTO wiki_pages"
                "(path, title, summary, project, mtime, ts, source, verified_ts, status,"
                " stale_days) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (path, title[:14], summary[:40], project[:20], mtime, time.time(),
                 (meta.get("source") or "自撰")[:40], float(meta.get("verified_ts") or 0),
                 meta.get("status") or "active", int(meta.get("stale_days", -1))))
        c.close()
        done += 1
    return done
