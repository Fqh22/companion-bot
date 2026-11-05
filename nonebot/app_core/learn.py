"""学习层：复盘、周报、间隔重复、技能、笔记、好奇/纠正清单。"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app_core import paths as _paths

from app_core import profile as profile_mod

log = logging.getLogger("qqbot.learn")

LEARN = _paths.LEARN
PROFILE = LEARN / "self_profile.md"
# 结构化画像：md 继续留着（kb 索引 / review 回读都用它），json 是机器读的那份
PROFILE_JSON = LEARN / "profile.json"
CORRECTIONS = LEARN / "corrections.md"
CURIOUS = LEARN / "curious.md"
SKILLS = LEARN / "skills"
NOTES = LEARN / "notes"
RAW = LEARN / "raw"          # 对话原文（原始材料，不加工）
MEM_DB = _paths.MEM_DB
CHAT_DB = _paths.DATA / "chat.db"

SYSTEM_REVIEW = (
    "你在给自己记事。读今天真实发生过的对话，记下你真正会记住的东西："
    "他的偏好、正在忙的事、约好的事、你该怎么对他。"
    "另外挑出「他明确纠正过我、或者明确不喜欢我那样做」的，一条一句、能直接照着改。"
    "再记下「他提到过、但当时没讲清楚或你没搞懂的事」，一条一句，以后聊到再问。"
    "第一人称，像人记事，不要写「我是 AI 助手」这类身份说明；不确定的不编，寒暄和一次性闲聊不记。"
)
SYSTEM_PROFILE = (
    "你在维护一份「自我认知档案」，按固定字段记录他。"
    "只写长期有效的信息（他的处境、关系、项目、偏好、雷区），不写情绪表演、不写套话。"
    "每个字段一句话、不超过 40 字；确实不知道的字段给空字符串，不要编。"
    "不要用 QQ 号或数字称呼他，直接写「他」。字段是："
    + "、".join(profile_mod.FIELDS) + "。"
)

# 给模型看的字段模板：从 FIELDS 生成，避免两边写死两处、改一处漏一处
_PROFILE_SPEC = "{" + ",".join(
    f'"{k}":{{"text":"一句话，不超过 40 字；不知道就空字符串",'
    f'"confidence":0.0,"evidence":"今天对话里支撑它的原话片段",'
    f'"changed":false}}'
    for k in profile_mod.FIELDS) + "}"
# confidence 用 0~1：你从这几天的对话里**确实知道**才给 0.8 以上；
# 只是印象、或者从旧画像抄下来的，给 0.5 左右。低于 0.6 的字段不会被念出去。
# evidence 只写**今天这段对话里**他亲口说过的话。今天没聊到这个字段就写空字符串——
# 空表示「沿用旧值、但没有新证据」，这个字段会随时间自然过期。
# 编造证据比没有证据更糟：旧结论会一直生效，除非它过期。
# changed 判的是「今天这句有没有提出**新的或修正后**的信息」：
#   true  = 新信息、或者和旧画像不一致（换工作、搬家、改了偏好）；
#   false = 只是在复述旧画像里已有的结论，措辞变了不算。
# 判 false 时程序会**保留旧文本**，只按你给的 confidence 记账——所以别为了凑数改措辞。
SYSTEM_WEEKLY = (
    "你是一个长期陪伴用户的 AI 助手，每周给主人写一份简短的「记忆周报」。"
    "基于这一周真实发生的记忆，用第一人称写，像朋友复盘：我这周记住了什么、"
    "你最近在意什么、我打算跟进什么、我这周有什么变化。"
    "另外必须单独检查一件事：对照【他纠正过我、不能再犯的】，看这周我有没有再犯；"
    "再犯了就具体说哪次，没犯就一句带过，不要报喜不报忧。"
    "不要罗列流水账，不要编造；450 字以内，分 3-4 个小段落，语气自然、带点幽默但不油腻。"
)
SYSTEM_RECALL = (
    "你翻到几段很久没想起的记忆。用第一人称写一段短短的回忆，2-4 句，"
    "像突然想起来那样：先说你想起什么，再说它为什么还留着。"
    "不要罗列条目、不要标题、不要 Markdown、不要编造细节。只输出这段回忆。"
)


# ---------- 清单读写 ----------
def profile_fields() -> dict[str, str]:
    """当前结构化画像。文件不在（还没跑过复盘）就返回空。"""
    return profile_mod.load(PROFILE.parent / "profile.json")


def profile_conf() -> dict[str, float]:
    return profile_mod.load_conf(PROFILE.parent / "profile.json")


def profile_seen() -> dict[str, float]:
    return profile_mod.load_seen(PROFILE.parent / "profile.json")


def profile_path() -> Path:
    return PROFILE.parent / "profile.json"


def profile_previous() -> dict[str, dict]:
    return profile_mod.load_previous(PROFILE.parent / "profile.json")


def _profile_block_for_review() -> str:
    """给复盘模型看的当前画像：每条都带上「多久没证据、置信度、上次是什么」。

    为什么要带年龄：不带的话，模型看到旧画像只会照着抄；带上「这条 40 天没证据了」，
    它才有机会判断「该更新还是该保留」。上次的值也一并给，渐变（换了方向但没明说）
    才能被看见。
    """
    fields = profile_fields()
    if not fields:
        try:
            return PROFILE.read_text(encoding="utf-8").strip() if PROFILE.exists() else ""
        except Exception:  # noqa: BLE001
            return ""
    conf, seen, prev = profile_conf(), profile_seen(), profile_previous()
    lines = []
    for k in profile_mod.FIELDS:
        v = fields.get(k)
        if not v:
            continue
        d = profile_mod.age_days(seen.get(k))
        age = "刚有证据" if d is None else (f"{d:.0f} 天前" if d >= 1 else "今天")
        tail = ""
        p = prev.get(k) or {}
        if p.get("text"):
            pd = profile_mod.age_days(p.get("at"))
            when = "最近" if (pd is None or pd < 1) else f"{pd:.0f} 天前"
            tail = f"｜上一次是「{p['text'][:18]}」（{when}改的）"
        lines.append(f"- {k}：{v}（{age}有证据，置信度 "
                     f"{float(conf.get(k, 0.7)):.2f}{tail}）")
    return "\n".join(lines)


def read_list(path: Path) -> list[str]:
    try:
        if path.exists():
            return [ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.startswith("- ")]
    except Exception:
        pass
    return []


_CORR_CACHE: dict[tuple, list[str]] = {}


def _clean_key(s: str) -> str:
    """去掉场景标签和标点。**标签必须一起去掉**——不去的话
    「[文字]他明确」这种跨标签的套话会把三条不相干的教训连成一串。"""
    s = re.sub(r"^\s*\[(文字|图片|通用)\]\s*", "", str(s or ""))
    return re.sub(r"[\s，。、；：！？「」“”\"'（）()\-—…~,.;:!?]+", "", s)


def _grams(s: str, n: int) -> set[str]:
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def dedupe_items(items: list[str], min_len: int = 6, max_df: int = 3) -> list[str]:
    """同一条教训被换个说法写了几遍时，只留最近的那一条。

    **判据不是「句子像不像」，而是「有没有共享一个罕见的长短语」。**
    实测那三条「谎称记着了」的句子相似度只有 0.31 / 0.43 / 0.59，任何落在
    合理区间的阈值（≥0.55 才不误合别的事）都抓不到；但它们都含
    「记着了但实际」「说记就必须真」这类只出现两三次的 6 字以上短语。

    反过来，「他明确说」「他嫌我」「他明确嫌我」这种套话也会被判共享，
    所以卡两道：短语至少 `min_len` 个字、且全局只出现在 `max_df` 条以内。
    在线上 16 条真实数据上，这组参数合并 4 组、没有误合。
    """
    normed = [_clean_key(x) for x in items]
    n = len(items)
    if n < 2:
        return list(items)

    # 只收长度够、出现次数少的短语，倒排到条目上
    df: dict[str, int] = {}
    where: dict[str, list[int]] = {}
    for i, s in enumerate(normed):
        seen: set[str] = set()
        for L in range(min_len, min(len(s), 12) + 1):
            seen |= _grams(s, L)
        for g in seen:
            df[g] = df.get(g, 0) + 1
            where.setdefault(g, []).append(i)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for g, idx in where.items():
        if len(idx) < 2 or df[g] > max_df:
            continue
        base = idx[0]
        for other in idx[1:]:
            union(base, other)

    # 每个簇只留最后出现的那条（最近的写法）
    keep: dict[int, int] = {}
    for i in range(n):
        keep[find(i)] = i
    keep_set = set(keep.values())
    return [items[i] for i in range(n) if i in keep_set]


def recent_corrections(limit: int = 6, scene: str | None = None) -> list[str]:
    """他最近纠正过我的事。

    这些以前只写进文件、复盘时用来自查，**聊天时根本看不到**——所以纠正了一个月
    行为也不变。现在每轮注入 prompt（见 prompt.build_messages 的 lessons 段）。

    scene：按场景过滤。`image` 只取图片相关+通用，`text` 只取文字相关+通用。
    不分场景的话，「别瞎猜糊图」会被塞进纯文字对话里，占预算又没用——图片类
    提醒只在真有图的那轮才有意义。

    注入前先做一次罕见短语去重：6 个槽位里塞 3 条「别谎称记住」等于浪费一半。
    结果按文件 mtime 缓存——这份清单一天只变一次（03:40 复盘），
    每轮重算 16 条的两两比较纯属浪费。
    """
    try:
        mtime = CORRECTIONS.stat().st_mtime
    except Exception:
        mtime = 0.0
    key = (str(CORRECTIONS), mtime, scene, int(limit))
    hit = _CORR_CACHE.get(key)
    if hit is not None:
        return list(hit)

    items = read_list(CORRECTIONS)
    if scene:
        items = [it for it in items if correction_scene(it) in ("general", scene)]
    items = dedupe_items(items)
    out = items[-max(1, int(limit)):]
    _CORR_CACHE.clear()
    _CORR_CACHE[key] = list(out)
    return out


def correction_scene(item: str) -> str:
    """读一条纠正的场景标签。没标签的老条目按「通用」算。"""
    t = (item or "").strip()
    for tag, scene in (("[图片]", "image"), ("[文字]", "text"), ("[通用]", "general")):
        if t.startswith(tag):
            return scene
    return "general"


def write_list(path: Path, title: str, items: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n" + "\n".join(f"- {x}" for x in items) + "\n", encoding="utf-8")


def _list_key(item: str) -> str:
    """去掉场景标签和标点，只留用于比较的骨架。"""
    t = re.sub(r"^[\[【]\s*(图片|文字|通用)\s*[\]】]", "", (item or "").strip())
    return re.sub(r"[，。、；：！？：「」“”\"'（）()【】\[\]\s]", "", t)


def too_similar(a: str, b: str, threshold: float = 0.62) -> bool:
    """两条是不是在说同一件事。

    复盘每天写 3-5 条，模型很爱换句话重复同一个意思（「记着了」那件事写过两遍）。
    精确匹配拦不住，纯靠 cap 又会把真教训挤出去，所以按相似度合并。

    两道保险，避免把不同的事误合：
    - 长度差太多不比较（短句容易被长句"吸收"）；
    - 阈值 0.62 是在线上 14 条上试出来的，低于它会开始吃掉不同的条目。
    """
    import difflib

    ka, kb = _list_key(a), _list_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((len(ka), len(kb)))
    # 短条目只认精确匹配：「问题三」和「问题一」相似度就够高，但它们是两件事。
    # 模糊匹配只对够长的句子有意义，那里才有足够的语义信息。
    if short < 8:
        return ka == kb
    if long_ and short / long_ < 0.55:
        return False
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= threshold


def merge_list(path: Path, title: str, items: list[str], cap: int = 24) -> list[str]:
    """合并进一个列表文件，保留最近 cap 条。

    上限一路从 10 提到 24：端到端跑一次复盘就新增 5 条，10 的容量两天就满，
    而最先被丢掉的往往是最早也最关键的几条（「别只顺着我说话」）。
    注入时只取 6 条，所以存得多不等于贵。

    合并规则：**相似即重复**。不光比较新来的，整表也重新去一次重——
    这样历史上已经写重的条目会在下一次写入时自动收敛。
    """
    cur = read_list(path)
    for it in items:
        it = (it or "").strip()[:120]
        if it and not any(too_similar(it, x) for x in cur):
            cur.append(it)
    deduped: list[str] = []
    for it in cur:
        if not any(too_similar(it, x) for x in deduped):
            deduped.append(it)
    if len(deduped) < len(cur):
        log.info("%s 合并了 %d 条相似条目", path.name, len(cur) - len(deduped))
    cur = deduped
    if len(cur) > cap:
        dropped = len(cur) - cap
        log.warning("列表 %s 超出上限，丢弃最旧 %d 条", path.name, dropped)
    cur = cur[-cap:]
    write_list(path, title, cur)
    return cur


def resolve_curious(items: list[str]) -> int:
    cur = read_list(CURIOUS)
    if not cur or not items:
        return 0

    def hit(line: str) -> bool:
        return any(it and (it in line or line in it) for it in items)

    remain = [x for x in cur if not hit(x)]
    n = len(cur) - len(remain)
    if n:
        write_list(CURIOUS, "还没搞懂的事", remain)
    return n


def _extract_json(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ---------- 记忆读取 ----------
def _documents_since(days: float) -> list[str]:
    since = datetime.now() - timedelta(days=days)
    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT text, created_at FROM documents ORDER BY id DESC LIMIT 200").fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for text, created in rows:
        try:
            dt = datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if dt >= since and (text or "").strip():
            out.append(text.strip())
    return out


# ---------- 复盘 ----------
def _day_context(days: float = 1) -> str:
    """复盘/周报的补充信号：情绪、对话量、提醒完成情况。

    原来只看记忆文档，等于只看到「结论」，看不到「那天是什么状态」。
    """
    parts = []
    try:
        from app_core import mood

        # 先 settle 再读：凌晨复盘用的是「此刻」的状态，而不是上一条消息时的快照。
        # 不 settle 的话，03:40 的复盘会把昨晚 23:50 的情绪当成今天的状态写进学习资料。
        m = mood.settle(mood.load())
        parts.append("情绪：心情 %+.2f，精力 %.2f，互动 %.2f"
                     % (m.get("valence", 0), m.get("energy", 0), m.get("social", 0)))
    except Exception:
        pass
    since = time.time() - days * 86400
    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=10)
        n = con.execute("SELECT COUNT(*) FROM messages WHERE ts>=?", (since,)).fetchone()[0]
        con.close()
        parts.append(f"对话量：{n} 条消息")
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute(
            "SELECT text, sent FROM reminders WHERE due_ts>=? ORDER BY due_ts",
            (since,)).fetchall()
        con.close()
        if rows:
            done = sum(1 for _, s in rows if s)
            parts.append(f"提醒：{len(rows)} 条到期，已发出 {done}")
            for text, sent in rows[:5]:
                parts.append(f"  - {text}（{'已提醒' if sent else '未提醒'}）")
    except Exception:
        pass
    return "\n".join(parts)


def normalize_correction(item: str) -> str:
    """给一条纠正补场景标签。

    复盘模型**可能漏标**。漏了就按「通用」存，而不是留成无标签——无标签在读取时
    会退化成通用、两类都注入，等于分流失效。写入时补上，分流才是确定的。
    """
    t = (item or "").strip()[:120]
    if not t:
        return ""
    if re.match(r"^[\[【]\s*(图片|文字|通用)", t):
        return t
    return "[通用] " + t


def apply_review(data: dict, day: str | None = None) -> dict:
    """把复盘产出的 JSON 落到文件。**不调模型**，所以写入规则可以离线测。

    拆出来的原因：review() 里"调模型"和"写文件"混在一起，导致写入规则
    （纠正必须带场景标签、列表上限、去重）一直没被测过——只测了读的那一侧。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    learned = str(data.get("learned") or "").strip()
    raw_profile = data.get("profile")
    fields = profile_mod.parse(raw_profile)
    conf = profile_mod.parse_conf(raw_profile)
    evidence = profile_mod.parse_evidence(raw_profile)
    # 老格式（一段自由文本）仍然收下，写进 md；json 这边就当这轮没给
    legacy = ""
    if not fields and isinstance(raw_profile, str):
        legacy = raw_profile.strip()[:1200]
    out = {"learned": bool(learned), "profile": bool(fields or legacy),
           "corrections": 0, "curious": 0, "resolved": 0}
    if not learned and not bool(fields or legacy):
        return out
    LEARN.mkdir(parents=True, exist_ok=True)
    (LEARN / f"{day}.md").write_text(
        f"# {day} 复盘\n\n## 今天学到\n{learned}\n\n## 自我认知更新\n"
        f"{profile_mod.render_md(fields) or legacy}\n",
        encoding="utf-8")
    if fields:
        # 合并而不是整份替换：一天没聊工作，不代表他不在找工作。
        # 路径从 PROFILE 推出来，测试里只改 PROFILE 也能正确重定向。
        # 写入规则全在 profile.update_state（纯函数）：changed=false 保留旧文本、
        # true 才改写并记 previous、有证据才刷新 seen、过期从库里删。
        json_path = PROFILE.parent / "profile.json"
        now = time.time()
        state = profile_mod.update_state(
            profile_mod.load(json_path), profile_mod.load_conf(json_path),
            profile_mod.load_seen(json_path), profile_mod.load_previous(json_path),
            fields, conf, evidence, profile_mod.parse_changed(raw_profile), now)
        merged = state["fields"]
        profile_mod.save(json_path, merged, state["confidence"], state["seen"],
                         state["previous"],
                         extra={"updated_at": now, "day": day})
        if state["dropped"]:
            out["profile_dropped"] = state["dropped"]
            log.info("[learn] 画像过期删除：%s", "、".join(state["dropped"]))
        PROFILE.write_text(profile_mod.render_md(merged), encoding="utf-8")
    elif legacy:
        PROFILE.write_text(legacy, encoding="utf-8")

    corr = [normalize_correction(str(c)) for c in (data.get("corrections") or [])]
    corr = [c for c in corr if c]
    if corr:
        merge_list(CORRECTIONS, "他纠正过我的", corr)
    out["corrections"] = len(corr)

    cur = [str(c).strip() for c in (data.get("curious") or []) if str(c).strip()]
    if cur:
        merge_list(CURIOUS, "还没搞懂的事", cur)
    out["curious"] = len(cur)

    resolved = [str(x).strip() for x in (data.get("resolved_curious") or [])
                if str(x).strip()]
    if resolved:
        out["resolved"] = resolve_curious(resolved)
    return out


async def review(core) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    memories = _documents_since(1)
    if not memories:
        return f"{today} 没有新记忆，跳过复盘"
    profile = _profile_block_for_review()
    curious_now = read_list(CURIOUS)
    joined = "\n".join(f"- {m[:220]}" for m in memories[:40])
    prompt = (
        f"【我现在的自我认知】\n{profile or '（还没有）'}\n\n"
        f"【{today} 的状态】\n{_day_context(1) or '（没有记录）'}\n\n"
        f"【{today} 的记忆】\n{joined}\n\n"
        f"【我还没搞懂的事】\n" + ("\n".join(f"- {x}" for x in curious_now) or "（没有）") + "\n\n"
        f"【已经记下的纠正（别重复写——换个说法也算重复）】\n"
        + ("\n".join(f"- {x}" for x in read_list(CORRECTIONS)[-24:]) or "（还没有）") + "\n\n"
        "请输出 JSON，不要输出别的：\n"
        '{"learned":"3-6 条可复用的判断规则，写成「当…时，做…（因为…）」或「别做…，因为…」；不要写「今天聊了…」这种流水",'
        f'"profile":{_PROFILE_SPEC},'
        '"corrections":["他明确纠正过我的、或者明确不喜欢我的做法，每条一句，'
        '开头必须带场景标签：[图片] 看图/识图判断错，[文字] 说话方式或内容错，[通用] 其他；'
        '没有就空数组"],'
        '"curious":["他提到但我没搞懂的事，每条一句；没有就空数组"],'
        '"resolved_curious":["今天已经搞懂的、上面「我还没搞懂的事」里的条目；没有就空数组"]}'
    )
    raw = await core.complete_summary(
        [{"role": "system", "content": SYSTEM_REVIEW + SYSTEM_PROFILE},
         {"role": "user", "content": prompt}],
        max_tokens=900,
    )
    data = _extract_json(raw)
    res = apply_review(data, today)
    if not res["learned"] and not res["profile"]:
        return "复盘失败：模型没有给出可用结果"
    return f"复盘完成（{len(memories)} 条记忆，纠正 {res['corrections']} 条）"


# ---------- 周报 ----------
def quality_metrics(days: int = 7) -> dict:
    """自动算几个「回复好不好」的指标，给周报用。

    为什么要自动算：靠人工巡检会漏，而且事后想不起是哪一周开始变差的。
    全部来自 chat.db 的现成数据，不额外花 token。
    """
    cut = time.time() - days * 86400
    out: dict = {}
    try:
        conn = sqlite3.connect(CHAT_DB, timeout=10)
        try:
            u = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user' AND ts>?",
                (cut,)).fetchone()[0]
            a, alen = conn.execute(
                "SELECT COUNT(*), COALESCE(AVG(LENGTH(content)),0) FROM messages"
                " WHERE role='assistant' AND ts>?", (cut,)).fetchone()
            ulen = conn.execute(
                "SELECT COALESCE(AVG(LENGTH(content)),0) FROM messages"
                " WHERE role='user' AND ts>?", (cut,)).fetchone()[0]
            dup = conn.execute(
                "SELECT COUNT(*) FROM (SELECT REPLACE(REPLACE(content,'？',''),'。','') k,"
                " COUNT(*) c FROM messages WHERE role='user' AND ts>?"
                " GROUP BY k HAVING c>1)", (cut,)).fetchone()[0]
            tok = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage"
                " WHERE ts>?", (cut,)).fetchone()[0]
            out = {
                "用户消息": int(u or 0), "回复": int(a or 0),
                "平均回复字数": round(float(alen or 0)),
                "平均用户字数": round(float(ulen or 0)),
                "长度比": round(float(alen or 0) / float(ulen or 1), 1),
                "重复提问条数": int(dup or 0),
                "token": int(tok or 0),
            }
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.debug("指标计算失败：%s", e)
    try:
        out["纠正清单条数"] = len(read_list(CORRECTIONS))
    except Exception:  # noqa: BLE001
        pass
    return out


async def weekly(core) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    memories = _documents_since(7)
    if not memories:
        return "这周没有新记忆，周报跳过"
    profile = PROFILE.read_text(encoding="utf-8").strip() if PROFILE.exists() else ""
    corr = read_list(CORRECTIONS)
    cur = read_list(CURIOUS)
    joined = "\n".join(f"- {m[:220]}" for m in memories[:60])
    prompt = (
        f"【我现在的自我认知】\n{profile or '（还没有）'}\n\n"
        f"【他纠正过我、不能再犯的】\n" + ("\n".join(f"- {x}" for x in corr) or "（没有）") + "\n\n"
        f"【我还没搞懂的事】\n" + ("\n".join(f"- {x}" for x in cur) or "（没有）") + "\n\n"
        f"【这一周的状态】\n{_day_context(7) or '（没有记录）'}\n\n"
        f"【这一周（{today} 往前 7 天）的记忆】\n{joined}\n\n"
        "写一份发给主人的记忆周报，并检查这周有没有再犯纠正清单里的错。"
    )
    text = await core.complete_summary(
        [{"role": "system", "content": SYSTEM_WEEKLY},
         {"role": "user", "content": prompt}],
        max_tokens=900,
    )
    if not text:
        return "周报失败：模型没有输出"
    m = quality_metrics(7)
    if m:
        text += ("\n\n## 这周的客观指标\n"
                 + "\n".join(f"- {k}：{v}" for k, v in m.items()))
    # 影子这周：七路样本的汇总。之前只有 ops/shadow-eval.py 能手动看，
    # 没人跑就等于没记——现在跟着周报一起出来。
    try:
        from app_core import shadow as shadow_mod

        _snap = shadow_mod.weekly_summary(7)
        _prev = shadow_mod.rotate_snapshot(_snap)
        _sl = shadow_mod.summary_lines(7, prev=_prev)
        if _sl:
            text += ("\n\n## 影子这周（只记录，没改行为）\n"
                     + "\n".join("- " + x for x in _sl))
    except Exception as e:  # noqa: BLE001
        log.debug("影子周汇总失败：%s", e)
    # 运维两件事：异地备份、恢复演练。周级提示，不天天烦。
    try:
        from app_core import maintenance as maint_mod

        _ops = maint_mod.ops_status()
        if _ops:
            text += ("\n\n## 运维\n" + "\n".join("- " + x for x in _ops))
    except Exception as e:  # noqa: BLE001
        log.debug("运维状态汇总失败：%s", e)
    LEARN.mkdir(parents=True, exist_ok=True)
    (LEARN / f"weekly-{today}.md").write_text(text + "\n", encoding="utf-8")
    return text


# ---------- 间隔重复 ----------
def review_candidates(min_importance: float = 0.6, min_age_days: float = 3,
                      limit: int = 5) -> list[tuple]:
    """按「重要性 × 遗忘程度」挑要复习的原子事实。

    上一版按 importance DESC 排序，而 reinforce 每次又往上加，
    头部几条会一直霸榜、永远复习那几条。现在用分数：
    importance × exp(-距上次强化天数 / 30)，复习过就掉分，自然轮到别的。
    """
    now = time.time()
    cutoff = now - min_age_days * 86400
    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute(
            "SELECT id, content, importance, COALESCE(last_reinforced_at,0) FROM memory_atoms "
            "WHERE status='active' AND importance >= ? AND COALESCE(last_reinforced_at,0) < ? "
            "LIMIT 500", (min_importance, cutoff)).fetchall()
        con.close()
    except Exception:
        return []
    scored = []
    for rid, content, imp, last in rows:
        imp = float(imp or 0.5)
        days = (now - float(last)) / 86400.0 if last else 999.0
        scored.append((imp * math.exp(-max(0.0, days) / 30.0), rid, content, imp))
    scored.sort(key=lambda x: -x[0])
    return [(r[1], r[2], r[3]) for r in scored[:limit]]


def reinforce(ids: list[int], boost: float = 0.02) -> int:
    """复习一次：记次数、更新时间、小幅提权。

    提权改用「朝 1.0 逼近」的形式，避免每次 +0.02 让老条目无限涨到 1.0。
    """
    if not ids:
        return 0
    now = time.time()
    try:
        con = sqlite3.connect(MEM_DB, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        with con:
            for i in ids:
                con.execute(
                    "UPDATE memory_atoms SET reinforcement_count=COALESCE(reinforcement_count,0)+1,"
                    "last_reinforced_at=?, last_accessed_at=?, "
                    "importance=MIN(1.0, importance + ? * (1.0 - importance)) "
                    "WHERE id=?", (now, now, boost, i))
        con.close()
        return len(ids)
    except Exception:
        return 0


async def spaced_review(core, limit: int = 5) -> str:
    rows = review_candidates(limit=limit)
    if not rows:
        return "没有需要复习的记忆"
    ids = [int(r[0]) for r in rows]
    n = reinforce(ids)
    lines = [f"{i+1}. {str(r[1])[:300]}" for i, r in enumerate(rows)]
    text = await core.complete_summary(
        [{"role": "system", "content": SYSTEM_RECALL},
         {"role": "user", "content": "这些是很久没想起的记忆：\n" + "\n".join(lines) + "\n\n写一段回忆。"}],
        max_tokens=500,
    )
    day = datetime.now().strftime("%Y-%m-%d")
    body = [f"# {day} 回忆\n", f"\n今天复习了 {n} 条记忆。\n"]
    body.append(f"\n{text}\n" if text else "\n" + "\n".join(f"- {r[1][:200]}" for r in rows) + "\n")
    LEARN.mkdir(parents=True, exist_ok=True)
    (LEARN / f"recall-{day}.md").write_text("".join(body), encoding="utf-8")
    return f"复习了 {n} 条记忆"


# ---------- 技能 ----------
def skill_save(name: str, trigger: str, steps: str) -> str:
    name = (name or "").strip()[:40]
    if not name:
        return "技能名不能为空"
    SKILLS.mkdir(parents=True, exist_ok=True)
    text = (f"# {name}\n\n触发：{trigger or name}\n"
            f"更新时间：{datetime.now():%Y-%m-%d %H:%M}\n\n{steps.strip()}\n")
    p = SKILLS / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    _touch_wiki(p)
    return f"技能已保存：{name}"


def skill_list() -> list[str]:
    try:
        return sorted(p.stem for p in SKILLS.glob("*.md"))
    except Exception:
        return []


def skill_read(name: str) -> str:
    p = SKILLS / f"{name}.md"
    return p.read_text(encoding="utf-8")[:3000] if p.exists() else f"没有这个技能：{name}"


def skill_match(text: str, max_skills: int = 2) -> list[str]:
    hits = []
    for p in SKILLS.glob("*.md") if SKILLS.exists() else []:
        try:
            raw = p.read_text(encoding="utf-8")
        except Exception:
            continue
        m = re.search(r"^触发[：:]\s*(.+)$", raw, re.M)
        if not m:
            continue
        triggers = [x.strip() for x in re.split(r"[,，、\s]+", m.group(1)) if x.strip()]
        score = sum(1 for t in triggers if t and t in text)
        if score:
            hits.append((score, raw))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:max_skills]]


# ---------- 笔记 ----------
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


def note_write(title: str, content: str, tag: str = "", project: str = "",
               source: str = "自撰", stale_days: int = 0) -> str:
    """写学习笔记。project 会写进文件头，知识库按项目过滤时用得上。

    stale_days：这条知识多久会过时（天），**由写笔记的模型自己判断**，
    0 表示长期有效。写进文件头，索引和 kb_read 据此提醒「可能过时」。
    为什么不让关键词判：字面匹配分不清「价格」是信息还是方法论（实测误判率 2/3）。
    """
    title = (title or "").strip()[:60] or "未命名笔记"
    content = (content or "").strip()
    if not content:
        return "内容为空"
    slug = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", title).strip("-")[:30] or "note"
    NOTES.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    # 同名标题跨天也写进同一页：知识应该沉淀，不是每天新建一份
    same = sorted(NOTES.glob(f"*{slug}.md"))
    p = same[-1] if same else NOTES / f"{now:%Y-%m-%d}-{slug}.md"
    if p.exists():
        with p.open("a", encoding="utf-8") as f:
            # 补充段带完整日期：老的只写「## 15:30 补充」，跨天以后根本不知道是哪天补的
            f.write(f"\n\n## {now:%Y-%m-%d %H:%M} 补充（未验证）\n\n{content}\n")
        _touch_wiki(p)
        return f"已追加到 {p.name}"
    proj = (project or "").strip()[:20]
    if not proj:
        t = (tag or "").strip()
        proj = t if t and t not in ("未分类", "网页学习") else "通用"
    p.write_text(
        f"# {title}\n\n- 日期：{now:%Y-%m-%d %H:%M}\n- 标签：{tag or '未分类'}\n"
        f"- 项目：{proj}\n- 来源：{source or '自撰'}\n- 最后验证：未验证\n- 状态：active\n"
        f"- 保质期：{int(stale_days)}天\n\n"
        f"{content}\n",
        encoding="utf-8")
    _touch_wiki(p)
    return f"已记下：{p.name}"


def _touch_wiki(path: Path) -> None:
    """写完笔记立刻在索引里占位（写穿）。

    延迟导入是为了避开循环依赖：wiki 在模块级 import learn，
    而这里只在真正写完的那一刻才需要 wiki。

    不做的话：note_write 只落文件，索引要等下一小时 refresh 才更新——
    模型刚写完的知识，在下一轮 kb_read 打开会说「没找到这一页」。
    """
    try:
        from app_core import wiki as _wiki

        _wiki.upsert_now(path)
    except Exception:
        pass


def note_list(limit: int = 10) -> list[str]:
    try:
        files = sorted(NOTES.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]
        return [p.name for p in files]
    except Exception:
        return []


# ---------- 对话原文归档 ----------
def _day_messages(day: str) -> list[tuple[str, str, float]]:
    con = sqlite3.connect(CHAT_DB, timeout=15)
    try:
        return con.execute(
            "SELECT role, content, ts FROM messages "
            "WHERE date(ts, 'unixepoch', 'localtime') = ? ORDER BY id", (day,)).fetchall()
    finally:
        con.close()


def archive_raw(day: str = "") -> str:
    """把某天的对话原文落盘到 `learn/raw/YYYY-MM-DD.md`。

    为什么留原文：笔记、复盘、记忆全是模型二次加工的结果，出问题时要能回到
    「他当时到底说了什么」。这份不加工，也让 kb_search 能搜到原始说法，
    而不是只搜到模型的转述。

    默认归档昨天（凌晨跑，当天已经完整），重复调用是覆盖，可以放心重跑。
    """
    day = day or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        rows = _day_messages(day)
    except Exception as e:  # noqa: BLE001
        return f"读会话库失败：{type(e).__name__}: {e}"
    if not rows:
        return f"{day} 没有对话记录"
    lines = [f"# {day} 对话原文", ""]
    kept = 0
    for role, content, ts in rows:
        text = (content or "").strip()
        if not text:
            continue
        who = "我" if role == "assistant" else "他"
        try:
            hhmm = datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except Exception:
            hhmm = "--:--"
        lines.append(f"[{hhmm}] {who}：{text}")
        kept += 1
    if not kept:
        return f"{day} 没有可归档的内容"
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{day}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"已归档 {day}：{kept} 条 → raw/{day}.md"
