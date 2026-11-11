"""LLM 工具定义与执行（OpenAI function calling 格式）。"""
from __future__ import annotations

from app_core import diary, kb, learn, meme, remind, web, wiki

TOOLS = [
    {"type": "function", "function": {
        "name": "note_write",
        "description": "把一条值得长期保留的知识或做法写成笔记。学到新知识、被教了一个做法、或搞清楚了之前没懂的问题时主动调用，不要等复盘。写成能照着用的手册，不是读书笔记：什么情况下用、具体怎么做、别怎么做以及为什么。超过 800 字就拆成几页，别堆在一页里。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "短而具体的标题"},
            "content": {"type": "string",
                        "description": "正文。分点写：① 什么时候用 ② 怎么做（带关键数字）③ 别做：…（因为…）"},
            "tag": {"type": "string",
                    "description": "可选分类，如 技术/他的偏好/面试；写项目名，他以后能按项目查到"},
            "stale_days": {"type": "integer",
                           "description": "这条知识多久会过时（天）。价格/行情/在售 14；版本/发布/新功能 30；公司或岗位近况、政策 90；长期有效的道理、他的偏好、做事方法 0（默认）"}},
            "required": ["title", "content"]}}},
    {"type": "function", "function": {
        "name": "note_list",
        "description": "列出最近写过的学习笔记。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索。需要查实时信息、新闻、最新版本、你不确定的事实，或者想确认某件事时用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索词，具体一点更好，1-128 字"},
            "count": {"type": "integer", "description": "返回几条，默认 5，最多 10"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_open",
        "description": "打开一个网页读正文；值得记的内容会提炼成学习笔记。",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "diary_read",
        "description": "读某天的日记，day 为 YYYY-MM-DD，留空读最近一篇。",
        "parameters": {"type": "object", "properties": {"day": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "kb_read",
        "description": "打开知识索引里的一页看全文。索引里列出的标题都能打开；用户问起之前记过的做法、结论时先用它。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "索引里的标题，也可以只写关键词"}},
            "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "kb_search",
        "description": "在知识库里按意思搜索（索引里没有合适的标题时用）。返回最相关的几段原文。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "要搜的内容，一句具体的话"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "按关键词检索长期记忆。想不起某件事时调用。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_read",
        "description": "把某条长期记忆的全文读出来。注入的「你记得的事」只有摘要（220 字），想还原细节、确认当时到底怎么说的，用这个。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "想找的那件事，一句话"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "remind_me",
        "description": "给对方设一个一次性提醒，到点主动发消息。用户说「明天九点提醒我」「一小时后叫我」时调用。时间要你算好，用 YYYY-MM-DD HH:MM 格式。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "到点要说的内容，简短具体"},
            "when": {"type": "string",
                     "description": "触发时间，YYYY-MM-DD HH:MM（24 小时制，本地时间）"}},
            "required": ["text", "when"]}}},
    {"type": "function", "function": {
        "name": "remind_list",
        "description": "列出还没到点的提醒（方括号里是编号）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "remind_cancel",
        "description": "取消一条还没到点的提醒。用户说「那个提醒不用了 / 取消掉」时调用；不知道编号就先调 remind_list 看。",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer",
                   "description": "提醒编号，就是 remind_list 里方括号中的数字"}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "meme_send",
        "description": "发一张表情包。他开玩笑、你自嘲、情绪明显、或者一句话太干时优先用它，别只用文字。query 写一句具体的话描述你想表达的情绪或场景（比如「被夸得很爽」「无语到笑」「累得想躺」「代码又报错了」），比只写分类名准得多；写完不用再描述这张图。每轮最多一张，别连着发。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词或分类，比如 开心/技术/困"}},
            "required": ["query"]}}},
]


# 只读工具：同一轮里可以并发执行。名单要跟着插件走——
# 只要有一个写操作混进并发，同名笔记/技能文件就可能互相覆盖。
READ_ONLY_TOOLS = frozenset({
    "web_search", "web_open", "kb_read", "kb_search", "memory_search", "memory_read",
    "diary_read", "skill_read", "skill_list", "note_list", "remind_list",
})

# 外人能用的工具：只给「不碰他的东西」的那几个。
# 判据不是读还是写，而是**碰不碰他的库**——note_list / kb_read / memory_read
# 都是只读，但读的全是他的笔记、知识和记忆，一样不能给。
# 发表情（meme_send）放进来是因为它只用一次、内容是他收藏的梗图，不涉及隐私。
# web_open 不能给：它会 learn.note_write（web.py），等于往他的知识库写东西，
# 而且这条路径不受「插件只做 IO」约束——陌生人不该有写入口。
PUBLIC_TOOLS = frozenset({"web_search", "meme_send"})


async def _note_write(core, args, images) -> str:
    try:
        sd = int(args.get("stale_days") or 0)
    except Exception:
        sd = 0
    return learn.note_write(args.get("title", ""), args.get("content", ""),
                            args.get("tag", ""), stale_days=max(0, sd))


async def _note_list(core, args, images) -> str:
    items = learn.note_list()
    return "\n".join(items) if items else "还没有学习笔记"


async def _web_search(core, args, images) -> str:
    return await web.search(args.get("query", ""), args.get("count", 5) or 5)


async def _web_open(core, args, images) -> str:
    return await web.web_open(core, args.get("url", ""))


async def _diary_read(core, args, images) -> str:
    day = (args.get("day") or "").strip()
    if not day:
        days = diary.list_days(1)
        day = days[0] if days else ""
    return diary.read(day) if day else "还没有日记"


async def _kb_read(core, args, images) -> str:
    return wiki.read(args.get("title", ""))


async def _kb_search(core, args, images) -> str:
    hits = await kb.search_meta(args.get("query", ""), 4)
    if not hits:
        return "知识库里没搜到相关内容"
    return "\n\n".join(
        (f"【{h['path']}{h.get('label') or ''}】\n" if h.get("path") else "") + h["text"]
        for h in hits)


async def _memory_read(core, args, images) -> str:
    return await core.memory.read_full(args.get("query", ""))


async def _memory_search(core, args, images) -> str:
    rows = await core.memory.recall(args.get("query", ""), 5)
    return "\n".join(f"- {r['text'][:200]}" for r in rows) if rows else "没想起相关的事"


async def _remind_me(core, args, images) -> str:
    return remind.add(args.get("text", ""), args.get("when", ""))


async def _remind_list(core, args, images) -> str:
    return remind.describe_pending()


async def _remind_cancel(core, args, images) -> str:
    try:
        rid = int(args.get("id") or 0)
    except Exception:
        return "编号要是数字，先调 remind_list 看方括号里的数字"
    return remind.cancel(rid)


async def _meme_send(core, args, images) -> str:
    path, mode, score = await meme.find_ex(args.get("query", ""))
    if not path:
        return "表情包库里没找到"
    if images is not None:
        images.append(path)
    if mode == "semantic":
        return f"已发了一张表情包（语义匹配 {score:.2f}）"
    return "已发了一张表情包（没找到贴合的，按分类挑的，可能不太对味）"


# 工具名 → 实现。**加工具只改两处**：TOOLS 里的 schema、这里的 HANDLERS，不再动 execute。
# 两边名字必须一致；模块加载时会自检，不一致直接抛错（比等测试红更早）。
HANDLERS = {
    "note_write": _note_write,
    "note_list": _note_list,
    "web_search": _web_search,
    "web_open": _web_open,
    "diary_read": _diary_read,
    "kb_read": _kb_read,
    "kb_search": _kb_search,
    "memory_read": _memory_read,
    "memory_search": _memory_search,
    "remind_me": _remind_me,
    "remind_list": _remind_list,
    "remind_cancel": _remind_cancel,
    "meme_send": _meme_send,
}


async def execute(core, name: str, args: dict, images: list[str] | None = None) -> str:
    """执行一个工具。images 是本轮回复要附带的图片，由调用方持有，避免跨会话串图。"""
    handler = HANDLERS.get(name)
    if handler is None:
        return f"未知工具：{name}"
    try:
        return await handler(core, args, images)
    except Exception as e:  # noqa: BLE001
        return f"工具执行失败：{type(e).__name__}: {e}"


# 启动自检：schema 与实现必须一一对应。漏了这里会立刻炸，而不是等到模型调工具才报未知。
_SCHEMA_NAMES = {t["function"]["name"] for t in TOOLS}
if _SCHEMA_NAMES != set(HANDLERS):
    raise RuntimeError(
        "工具 schema 与实现不一致：" + ", ".join(sorted(_SCHEMA_NAMES ^ set(HANDLERS))))



