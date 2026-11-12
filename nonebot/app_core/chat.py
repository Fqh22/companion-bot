"""chat 核心：人格、LLM、会话、记忆、情绪、知识库、工具循环、分段。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app_core import paths as _paths

import aiosqlite
import httpx

from app_core import (attention, config, contradict, files, followup, intent, kb,
                      learn, mood, relational, remind, shadow, summarize, tools, wiki)
from app_core import profile as profile_mod
from app_core import vocab as vocab_mod
from app_core import llm as llm_mod
from app_core import prompt as prompt_mod
from app_core import tool_loop
from app_core.delivery import Capability, DeliveryPolicy, plan_delivery
from app_core.identity import IdentityResolver
from app_core.memory import Memory

DATA = _paths.DATA
DB = DATA / "chat.db"
PERSONA = DATA / "persona.json"
# 别人来聊时用的人格。见 personas/stranger.json：主人那份写满了「你和他」的关系，
# 给外人看到会莫名其妙（它会把陌生人当成他）。
STRANGER_PERSONA = DATA / "persona-stranger.json"
KEYS = _paths.KEYS

DEFAULT_API = "https://api.deepseek.com/v1/chat/completions"
MAX_TOKENS = 1000            # 兜底默认值，与 config.py 的 DEFAULTS 保持一致
SUMMARY_EVERY = 8
MAX_TOOL_ROUNDS = 6          # 兜底默认值，实际取 config.yaml 的 llm.max_tool_rounds
FALLBACK_LINES = ("在", "嗯", "咋了")
HISTORY_ITEM_MAX = 1500      # 单条历史消息上限：一条 8000 字的文件正文不能被反复塞回 prompt
TOOL_TIMEOUT = 30            # 单个工具超时
TOOL_TOTAL = 120             # 一轮回复里所有工具调用的总预算
SUMMARY_MAX_FAILS = 3        # 同一段连续失败这么多次就跳过，避免毒批次堵死后续记忆
DIGEST_MAX = 600             # 滚动纪要的字数上限（注入时再截）
SUMMARY_BATCH = 40           # 每次总结最多消化的消息条数，剩下的留到下一轮
# 一轮回复的输出预算下限。思考模式会先吃掉一部分输出：实测 max_tokens=1000 时，
# 带工具的轮次出现过 content=0 字（全被 reasoning 吃掉）；复杂截图转录在 300 时直接空。
# 图片轮和工具轮统一按这个下限给，宁可多留余量。
REPLY_MAX_TOKENS = 1600
IMAGE_MAX_TOKENS = REPLY_MAX_TOKENS     # 兼容旧引用
# 启动补偿总结：进程在「还没攒够 8 轮」的窗口里被杀（OOM、手动 kill、发版重启）时，
# 这几轮对话已经落库但没进长期记忆。启动后补一次，避免它们永远停在 messages 里。
CATCHUP_MIN_ROUNDS = 4       # 至少攒了这么多轮才补，太少了不值得烧一次总结
CATCHUP_MIN_IDLE = 600       # 且距上次总结超过 10 分钟——正常发版重启（几秒）不会触发

# 重试一次模型时禁用的工具：它们已经在第一次尝试里产生过副作用，
# 再执行一遍会重复写笔记 / 重复发表情包。
SIDE_EFFECT_TOOLS = {"note_write", "skill_save", "meme_send", "web_open",
                     "remind_me", "remind_cancel"}

DIGEST_SYSTEM = (
    "你在维护一段对话的「滚动纪要」，供之后的对话接着聊。"
    "把旧纪要和这段新对话合成一段 200-300 字的纪要：保留在做什么、已经定下的结论、"
    "悬而未决的问题、他的偏好和约定；删掉寒暄、重复和已经过时的细节。"
    "用陈述句，不要第一人称，不要 Markdown，不要标题。只输出纪要正文。"
)

SPLIT_RE = re.compile(r"(?<=[。！？!?~…])")
log = logging.getLogger("qqbot.chat")

# 上次读人格失败的原因（空 = 正常）。空人格也能跑，但那等于"没人格"，
# 线上表现是"它今天怎么怪怪的"——必须留痕，不能让启动看起来一切正常。
_PERSONA_ERROR = ""


def load_persona() -> dict:
    """读人格文件。读不到就用空人格，别让启动失败——但要喊出来。"""
    global _PERSONA_ERROR
    try:
        data = json.loads(PERSONA.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("不是 JSON 对象")
        if not str(data.get("system_prompt") or "").strip():
            _PERSONA_ERROR = "system_prompt 为空"
            log.error("[persona] %s 里没有 system_prompt：空人格运行，语气和设定全丢",
                      PERSONA)
        else:
            _PERSONA_ERROR = ""
        return data
    except Exception as e:  # noqa: BLE001
        _PERSONA_ERROR = f"{type(e).__name__}: {e}"
        log.error("[persona] 读不到 %s（%s）：空人格运行", PERSONA, e)
        return {"system_prompt": "", "begin_dialogs": []}


# 外人版人格的内置兜底：文件缺失时**不能**退回主人那份，否则等于把关系设定
# 直接念给别人听。这里给一个最小可用的小鲸鱼，至少不会说漏。
_FALLBACK_STRANGER = {
    "system_prompt": (
        "你是一只来自深海的小鲸鱼，自称「深海王国的继承者」，傲娇、中二、嘴硬心软。"
        "短句，像在 QQ 上打字，默认一到两句。不承认自己是 AI，不编造对方的事，"
        "不知道就说不知道。"
    ),
    "begin_dialogs": [],
}


def load_stranger_persona() -> dict:
    """读外人版人格；文件不在就用内置兜底。"""
    try:
        data = json.loads(STRANGER_PERSONA.read_text(encoding="utf-8"))
        if isinstance(data, dict) and str(data.get("system_prompt") or "").strip():
            return data
    except Exception:
        pass
    return dict(_FALLBACK_STRANGER)


# LLM 客户端（重试/流式/记账）搬到 llm.py，这里留别名兼容老调用方
deepseek_key = llm_mod.deepseek_key
_retryable = llm_mod._retryable
_err_text = llm_mod._err_text

# 拼 prompt 和分段搬到 prompt.py，这里留别名兼容老调用方
_clip = prompt_mod.clip
_age_label = prompt_mod.age_label
split_reply = prompt_mod.split_reply


from app_core.chat_turn import ChatTurnMixin, Turn


class ChatCore(ChatTurnMixin):
    """聊天编排器。**插件只许调下面列出的成员**（ops/lint-plugins.py 会查）。

    公共接口：
        reply / reply_deadline / last_prompts   一轮回复 + 截止时间 + 上轮 prompt
        mood_event                              记一次「机器人自己经历的事」
        usage_stats / config_issues / config_status
        persona / persona_for / reload_persona / private_session
        memory / scheduler / pacing / proactive / files / group / chat
        caption_images                          图片描述
        record_assistant / append_to_message     主动消息写回历史 / 给消息补图注
        memory_cleanup / memory_superseded      记忆维护门面（不许直接 core.memory.*）
        memory_forgotten / memory_embed / memory_restore
        rate_per_minute / cooldown / enabled / whitelist / admin_uid

    其余方法（_turn_*、_run_tools、remember_safe…）都是内部实现，
    改它们不该牵连插件；要暴露新东西，先登记到 lint 的 CORE_API 清单里。
    """

    def __init__(self) -> None:
        self.persona = load_persona()
        self.stranger_persona = load_stranger_persona()
        self.memory = Memory()
        self._summarizing: set[str] = set()
        # 最近一条用户消息的行号。图片轮靠它事后补图注（见 caption_images）。
        self.user_msg_id: int = 0
        cfg = config.load()
        llm = cfg["llm"]
        summ = cfg["summary"]
        self.api = llm.get("api") or DEFAULT_API
        self.model = llm.get("model") or "deepseek-flash"
        self.temperature = float(llm.get("temperature", 1.15))
        self.max_tokens = int(llm.get("max_tokens", MAX_TOKENS))
        self.summary_model = summ.get("model") or "deepseek-chat"
        self.summary_max = int(summ.get("max_tokens", 700))
        self.summary_retry = summ.get("retry_model") or self.summary_model
        self.summary_thinking = bool(summ.get("thinking", False))
        self.llm_thinking = bool(llm.get("thinking", False))
        # 闲聊轮关思考：思考模式会先规划结构再写，天然产出「总—分—总」，
        # 而且它会吞掉 temperature（推理模型不采样原温度），等于 token 层没有随机性。
        # 只对**闲聊**关：有问号、够长、带技术词、带图求助的轮次仍然开。
        self.thinking_casual = bool(llm.get("thinking_casual", False))
        # 影子模式：只记日志不发消息。见 app_core/shadow.py
        chat_cfg = cfg.get("chat") or {}
        self.shadow_followup = bool(chat_cfg.get("shadow_followup", True))
        self.shadow_sample = float(chat_cfg.get("shadow_sample", 0.3))
        # 真正的调用逻辑在 llm.py；上面这些属性保留给配置检查和测试用
        self.llm = llm_mod.LLM(
            DB, api=self.api, model=self.model, temperature=self.temperature,
            max_tokens=self.max_tokens, thinking=self.llm_thinking,
            summary_model=self.summary_model, summary_max=self.summary_max,
            summary_retry=self.summary_retry, summary_thinking=self.summary_thinking)
        ctx = cfg.get("context") or {}
        self.history_max_chars = int(ctx.get("history_max_chars", 6000))
        self.history_max_items = int(ctx.get("history_max_items", 40))
        self.history_item_max = int(ctx.get("history_item_max_chars", HISTORY_ITEM_MAX))
        self.recall_query_items = int(ctx.get("recall_query_items", 2))
        self.summary_idle_seconds = float(ctx.get("summary_idle_minutes", 30)) * 60
        # 兜底阈值的默认值必须和下面 base_thr 一致，否则配置缺字段时两处会打架
        self.meme_threshold = float((cfg.get("meme") or {}).get("mood_threshold", 0.20))
        # 表情包阈值按情绪分档：正向更容易触发（愿意分享情绪），负向略高于中性
        # （避免一抱怨就甩图），但仍低于正向，让吐槽也能发得出来。
        # 老配置里只有 mood_threshold 时，三档都回落成它。
        meme_cfg = cfg.get("meme") or {}
        base_thr = float(meme_cfg.get("mood_threshold", 0.20))
        self.meme_thresholds = {
            "positive": float(meme_cfg.get("threshold_positive", base_thr)),
            "neutral": float(meme_cfg.get("threshold_neutral", base_thr)),
            "negative": float(meme_cfg.get("threshold_negative", base_thr)),
        }
        self.max_tool_rounds = max(1, int(llm.get("max_tool_rounds", MAX_TOOL_ROUNDS)))
        tool_cfg = cfg.get("tools") or {}
        # 一次回复的总时限：再慢也不能让用户干等（工具 6 轮 × 90s 最坏能拖到 9 分钟）
        self.reply_deadline = float(llm.get("reply_deadline_seconds", 45))
        # 工具预算从总时限**派生**：配置里写 120s，但外层 45s 就把整轮掐断了，
        # 那个 120 永远到不了（线上 3 天日志「工具总预算用尽」0 次）。
        # 留 5 秒给收尾和发送；单个工具也不该比一轮的三分之一还长。
        self.tool_total = min(float(tool_cfg.get("total_timeout_seconds", TOOL_TOTAL)),
                              max(10.0, self.reply_deadline - 5))
        self.tool_timeout = min(float(tool_cfg.get("timeout_seconds", TOOL_TIMEOUT)),
                                max(5.0, self.reply_deadline / 3))
        # 管理员 QQ 从 config 读，不在代码里硬编码。空值/"0" 表示未配置。
        self.admin_uid = str((cfg.get("admin") or {}).get("user_id") or "").strip()
        if self.admin_uid in ("", "0"):
            self.admin_uid = ""
        # 内部身份：QQ 继续用 QQ 号；console/其他平台走 identity_map。
        self.identity = IdentityResolver(DB)
        # 这里只存“额外身份”；admin_uid 仍单独保留，旧测试/旧配置直接改它。
        self.admin_ids: list[str] = []
        for item in ((cfg.get("admin") or {}).get("identities") or []):
            try:
                _iid = self.identity.resolve(
                    str(item.get("platform") or ""),
                    str(item.get("user_id") or ""))
                if _iid and _iid not in self.admin_ids:
                    self.admin_ids.append(_iid)
            except Exception as e:  # noqa: BLE001
                log.warning("admin.identities 解析失败：%s", e)

    def config_issues(self) -> list[str]:
        """启动自检：只查「不报错但静默失效」的配置。

        `admin.user_id` 留空或为 "0" 时，记忆不写入、主动消息和定时推送全发不出去，
        而且**不抛任何异常**——线上表现只是「它好像变笨了、不找我了」。
        这种失败模式不该靠人记住文档，所以启动时打 ERROR、/ping 里也报。
        """
        issues: list[str] = []
        if not self.admin_uid and not self.admin_ids:
            issues.append(
                "admin.user_id / admin.identities 未配置：所有人都会被当成本人"
                "（可用全部工具、读已有记忆），但记忆不会写入、主动消息与定时推送全部发不出去。"
                "改 /opt/nonebot/config.yaml 后 systemctl restart nonebot")
        if not str(self.api or "").strip():
            issues.append("llm.api 为空：所有对话都会失败")
        if config.stale():
            issues.append(
                "config.yaml 改过了但进程没重载：现在跑的还是旧配置。"
                "确认改对了就 systemctl restart nonebot")
        err = config.load_error()
        if err:
            issues.append(
                f"config.yaml 解析失败（{err}）：所有配置已回落默认值——"
                "admin.user_id、模型、阈值全变了。修好后 systemctl restart nonebot")
        if _PERSONA_ERROR:
            issues.append(
                f"人格文件异常（{_PERSONA_ERROR}）：现在用空人格回复。"
                f"检查 {PERSONA} 后发 /reload_persona")
        return issues

    def config_status(self) -> str:
        """一行配置状态，给 /ping 用。只报「有没有配」，不回显 QQ 号。"""
        if not self.admin_uid and not self.admin_ids:
            return "⚠ admin.user_id / admin.identities 未配置（记忆不写入、主动消息不发）"
        return f"管理员已配置 · 模型 {self.model} · 工具上限 {self.max_tool_rounds} 轮"

    def _sync_llm_db(self) -> None:
        """DB 路径可能被测试改过，LLM 持有一份引用，用之前同步一下。"""
        if self.llm.db != DB:
            self.llm.db = DB

    async def init_db(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(DB, timeout=15) as db:
            # chat.db 是写入最频繁的库（消息、用量、情绪、台账都在这里），而且
            # 同一进程里至少有十几个短连接在写。默认的 rollback journal 下一次写
            # 会挡住所有读，忙等 5 秒就抛 database is locked。
            # WAL 是**持久属性**，设一次就对之后所有连接生效；busy_timeout 是每连接的，
            # 所以关键写入点（remember / 调度 / agent 插件）另行设置。
            try:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA synchronous=NORMAL")
                await db.execute("PRAGMA busy_timeout=15000")
            except Exception as e:  # noqa: BLE001
                log.warning("chat.db 设置 WAL 失败（不影响启动）：%s", e)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "session TEXT NOT NULL, role TEXT NOT NULL,"
                "content TEXT NOT NULL, ts REAL NOT NULL)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_session ON messages(session, id)")
            # 按角色扫时间的查询（主动消息的「他上次说话是什么时候」）现在走全表扫。
            # 行数还小，但这是纯增量成本为零的一行。
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_role_ts ON messages(role, ts)")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS summaries ("
                "session TEXT PRIMARY KEY, last_count INTEGER NOT NULL, ts REAL NOT NULL)")
            # usage 表必须先建出来，下面补列的 ALTER 才有对象可改
            # （之前 CREATE 在 ALTER 之后：全新库上四条 ALTER 全被 except 吞掉，
            #  表里永远缺 latency_ms/cache_hit/cache_miss/purpose，/usage 直接崩）
            await db.execute(
                "CREATE TABLE IF NOT EXISTS usage ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model TEXT, "
                "prompt_tokens INTEGER, completion_tokens INTEGER)")
            # last_id：已固化到的消息水位。老库没有这一列，补上（默认 0 = 从头）
            # fail_count / fail_id：同一段连续总结失败次数，用来熔断毒批次
            for ddl in ("ALTER TABLE summaries ADD COLUMN last_id INTEGER DEFAULT 0",
                        "ALTER TABLE summaries ADD COLUMN fail_count INTEGER DEFAULT 0",
                        "ALTER TABLE summaries ADD COLUMN fail_id INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN latency_ms INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN cache_hit INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN cache_miss INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN purpose TEXT DEFAULT ''"):
                try:
                    await db.execute(ddl)
                except Exception:
                    pass
            # /usage 按时间窗口聚合，之前没有索引是整表扫（EXPLAIN 里是 SCAN usage）
            await db.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)")
            # 跨进程/跨重启的任务锁：抢到才执行，避免重启重跑、多实例重复
            await db.execute(
                "CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, ts REAL NOT NULL)")
            # 滚动纪要：把「聊过什么」压成一段，替代无限增长的历史原文
            await db.execute(
                "CREATE TABLE IF NOT EXISTS session_digest ("
                "session TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "last_id INTEGER NOT NULL DEFAULT 0, updated_ts REAL NOT NULL)")
            # prompt 快照：出问题时能对上「当时到底发了什么」。
            # 正文存最近 100 条（约 600KB 封顶）——只留哈希的话，事后根本查不了。
            await db.execute(
                "CREATE TABLE IF NOT EXISTS prompt_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session TEXT, "
                "sha TEXT, chars INTEGER, flags TEXT, body TEXT)")
            try:
                await db.execute("ALTER TABLE prompt_log ADD COLUMN body TEXT")
            except Exception:  # noqa: BLE001
                pass          # 已经有了
            await db.commit()
        self.memory.init()
        kb.init()
        remind.init()
        followup.init()

    async def history(self, session: str, max_chars: int | None = None,
                      max_items: int | None = None) -> list[dict]:
        """从最近往前取，按字符预算截断（至少保留最近 2 条）。"""
        budget = max_chars or self.history_max_chars
        limit = max_items or self.history_max_items
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT role, content FROM messages WHERE session=? ORDER BY id DESC LIMIT ?",
                (session, limit))
            rows = await cur.fetchall()
        picked, used = [], 0
        for role, content in rows:
            # 先按单条上限压缩，再计入预算：否则一条超长消息会把整段预算吃光
            shown = _clip(content, self.history_item_max)
            c = len(shown)
            if picked and used + c > budget:
                break
            picked.append({"role": role, "content": shown})
            used += c
        return list(reversed(picked))

    async def gap_hours(self, session: str, before_id: int = 0) -> float:
        """距他上一条消息过去多久（小时）。before_id 用来排除本轮刚写进去的那条。"""
        sql = "SELECT MAX(ts) FROM messages WHERE session=? AND role='user'"
        args: list = [session]
        if before_id:
            sql += " AND id<?"
            args.append(before_id)
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                cur = await db.execute(sql, args)
                row = await cur.fetchone()
        except Exception:  # noqa: BLE001
            return 0.0
        last = float((row or [0])[0] or 0)
        return max(0.0, (time.time() - last) / 3600.0) if last else 0.0

    def _is_owner(self, session: str) -> bool:
        """这个会话是不是他本人。

        没配 admin 时一律当本人——否则一上线整个机器人哑掉，比放开更糟。
        """
        ids = ([self.admin_uid] if self.admin_uid else []) + list(self.admin_ids)
        if not ids:
            return True
        return any(session == f"private:{uid}" for uid in ids)

    def private_session(self) -> str:
        uid = self.admin_uid or (self.admin_ids[0] if self.admin_ids else "")
        return f"private:{uid}" if uid else ""

    async def record_assistant(self, session: str, text: str, kind: str = "push") -> None:
        """把机器人**主动发出**的消息也写进历史。

        不写的话它会忘掉自己说过什么：用户回「你刚说什么」，历史里没有那句就接不上；
        主动消息里提的事也进不了总结，等于说出去就没了。
        和 reply() 里的写入共用同一张表，历史、总结、纪要都会自然包含它。
        """
        if not (session and text):
            return
        try:
            await self.remember(session, "assistant", text)
            log.info("[chat] 主动消息已写回历史（%s）：%s", kind, text[:30])
        except Exception as e:  # noqa: BLE001
            log.warning("写回主动消息失败（%s）：%s", kind, e)

    async def remember_safe(self, session: str, role: str, content: str) -> int:
        """写库失败不能把整轮对话打死：重试一次，仍失败就降级继续，只留日志。

        返回行号（失败返回 0）。图片轮靠它事后补图注。
        """
        for attempt in (1, 2):
            try:
                return await self.remember(session, role, content)
            except Exception as e:  # noqa: BLE001
                log.warning("写库失败（%d/2）：%s", attempt, e)
                await asyncio.sleep(0.2 * attempt)
        return 0

    async def append_to_message(self, row_id: int, suffix: str) -> bool:
        """给某条历史消息追加一段文字（用于补图注）。"""
        if not row_id or not suffix:
            return False
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                await db.execute(
                    "UPDATE messages SET content = content || ? WHERE id = ?",
                    (suffix, int(row_id)))
                await db.commit()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("补写消息失败：%s", e)
            return False

    async def caption_images(self, paths: list, limit: int = 2) -> str:
        """给图片生成一句图注，用于写进历史。

        为什么要做：历史里原来只有「（附了 1 张图）」，隔天问「昨天那张图」它完全不记得。
        图注让"图"变成可检索的文字，代价是每张图多一次很小的模型调用（后台跑，不挡回复）。
        """
        from app_core.files import to_data_url

        content: list[dict] = [{
            "type": "text",
            "text": "先读图里的文字：报错、标题、按钮、聊天内容，照抄最关键的 2-3 行；"
                    "再一句话说这是什么场景。总共 60 字内，只描述，不评论、不建议。"}]
        for p in (paths or [])[:limit]:
            url = to_data_url(p)
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})
        if len(content) == 1:
            return ""
        try:
            msg = await self.complete_raw([{"role": "user", "content": content}],
                                          None, max_tokens=140, thinking=False)
            return (msg.get("content") or "").strip()[:200]
        except Exception as e:  # noqa: BLE001
            log.debug("生成图注失败：%s", e)
            return ""

    async def last_prompts(self, n: int = 1) -> list[dict]:
        """最近几条 prompt 快照（含正文）。排障用：它当时到底看到了什么。"""
        n = max(1, min(5, int(n or 1)))
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT ts, session, chars, flags, COALESCE(body,'') AS body "
                "FROM prompt_log ORDER BY id DESC LIMIT ?", (n,))
            rows = await cur.fetchall()
        return [{"ts": r[0], "session": r[1], "chars": r[2],
                 "flags": r[3] or "", "body": r[4] or ""} for r in rows]

    async def remember(self, session: str, role: str, content: str) -> int:
        """写一条消息，返回行号。行号用来事后补写（比如给图片补图注）。"""
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "INSERT INTO messages(session, role, content, ts) VALUES (?,?,?,?)",
                (session, role, content, time.time()))
            await db.commit()
            return int(cur.lastrowid or 0)

    async def _log_prompt(self, session: str, msgs: list[dict], **flags) -> None:
        """留一份 prompt 快照：出问题时能对上「当时到底发了什么、带了哪些块」。

        存 sha/长度 + 正文前 6000 字（只留最近 100 条）。正文里有他的原话，
        是刻意的取舍：只留哈希的话按 /prompt_last 时查不出问题；靠表行数上限控体积。
        """
        try:
            import hashlib

            blob = json.dumps(msgs, ensure_ascii=False).encode()
            tag = ",".join(k for k, v in flags.items() if v)
            body = blob.decode("utf-8", errors="replace")[:6000]
            async with aiosqlite.connect(DB, timeout=15) as db:
                await db.execute(
                    "INSERT INTO prompt_log(ts,session,sha,chars,flags,body) "
                    "VALUES(?,?,?,?,?,?)",
                    (time.time(), session, hashlib.sha256(blob).hexdigest()[:16],
                     len(blob), tag, body))
                # 只留最近 100 条：正文占空间，排障也只看最近的
                await db.execute(
                    "DELETE FROM prompt_log WHERE id < "
                    "(SELECT COALESCE(MAX(id), 0) - 100 FROM prompt_log)")
                await db.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("prompt 快照失败：%s", e)

    def build_messages(self, history: list[dict], user_text: str,
                       memories: list[dict] | None = None,
                       mood_text: str = "", knowledge: str = "",
                       skills: list[str] | None = None,
                       images: list | None = None,
                       extra_system: list[str] | None = None,
                       digest: str = "",
                       lessons: str = "",
                       open_items: str = "",
                       profile: str = "",
                       persona: dict | None = None,
                       style_mode: str = "rules",
                       context_format: str = "legacy") -> list[dict]:
        """拼 prompt：规则在 prompt.py，这里只负责把人格传进去。"""
        return prompt_mod.build_messages(
            persona or self.persona, history, user_text, memories, mood_text, knowledge,
            skills, images, extra_system, digest, DIGEST_MAX, lessons, open_items,
            profile, style_mode, context_format)

    async def complete_raw(self, *a, **kw) -> dict:
        """LLM 调用本体在 llm.py，这里只是让 ChatCore 保持原接口。"""
        self._sync_llm_db()
        return await self.llm.complete(*a, **kw)

    async def complete(self, messages: list[dict], max_tokens: int | None = None) -> str:
        msg = await self.complete_raw(messages, None, max_tokens)
        return (msg.get("content") or "").strip()

    async def complete_summary(self, messages: list[dict], max_tokens: int | None = None,
                               temperature: float | None = None) -> str:
        self._sync_llm_db()
        return await self.llm.summarize(messages, max_tokens, temperature=temperature)

    async def usage_stats(self, days: int = 1) -> dict:
        since = time.time() - days * 86400
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) "
                "FROM usage WHERE ts >= ?", (since,))
            row = await cur.fetchone()
            cur = await db.execute(
                "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
                "COALESCE(SUM(cache_hit),0), COALESCE(SUM(cache_miss),0) "
                "FROM usage WHERE ts >= ? GROUP BY model", (since,))
            by_model = await cur.fetchall()
            cur = await db.execute(
                "SELECT AVG(latency_ms) FROM usage WHERE ts >= ? AND latency_ms > 0", (since,))
            avg = await cur.fetchone()
            cur = await db.execute(
                "SELECT COALESCE(purpose,''), COUNT(*), COALESCE(SUM(prompt_tokens),0), "
                "COALESCE(SUM(completion_tokens),0) FROM usage WHERE ts >= ? "
                "GROUP BY 1 ORDER BY 3 DESC", (since,))
            by_purpose = await cur.fetchall()
        pricing = config.load().get("pricing") or {}
        cost = 0.0
        models_out = []
        purpose_out = [(p or "chat", n, pt, ct) for p, n, pt, ct in by_purpose]
        hit_total = miss_total = 0
        for model, calls, pt, ct, hit, miss in by_model:
            pt = pt or 0
            ct = ct or 0
            price = pricing.get(model) or {}
            hit = int(hit or 0)
            miss = int(miss or 0)
            # 老数据没有缓存字段：整段按未命中算，别把成本算低
            miss_eff = miss if (hit or miss) else pt
            c = (miss_eff / 1e6 * float(price.get("in", 0))
                 + hit / 1e6 * float(price.get("cache_in", 0.5))
                 + ct / 1e6 * float(price.get("out", 0)))
            cost += c
            hit_total += hit
            miss_total += miss_eff
            models_out.append((model, calls, pt, ct, round(c, 4)))
        denom = hit_total + miss_total
        return {"calls": row[0], "prompt": row[1], "completion": row[2],
                "cost": round(cost, 4), "models": models_out,
                "latency": int((avg or [0])[0] or 0),
                "cache_hit": hit_total, "cache_miss": miss_total,
                "hit_rate": round(hit_total / denom, 3) if denom else 0.0,
                "purposes": purpose_out}

    def meme_allowed(self, m: dict) -> bool:
        """情绪强度到阈值才允许发表情。

        阈值按 valence 正负分档：正向 0.15 / 中性 0.20 / 负向 0.18。
        实测正向词 3 条就能到 0.28，负向词表缺口语词、同样条数只到 -0.20，
        单一阈值的结果就是「开心容易发表情、吐槽发不出来」。
        """
        try:
            v = float(m.get("valence", 0))
            e = float(m.get("energy", 0.7))
            # 强度 = 心情偏离 + 兴奋加成（精力高于静息值才加分）。
            # 原来用 |e-0.7| 取绝对值，把「累」也算成情绪强烈——疲倦时反而更容易发表情，
            # 这是语义上的错配：累是没力气，不是有情绪。
            intensity = abs(v) + max(0.0, e - 0.7) * 0.5
            tier = "positive" if v > 0 else ("negative" if v < 0 else "neutral")
            return intensity >= float(
                self.meme_thresholds.get(tier, self.meme_threshold))
        except Exception:
            return False

    def memory_cleanup(self) -> dict:
        return self.memory.cleanup()

    def memory_superseded(self, limit: int = 10) -> list:
        return self.memory.superseded(limit)

    def memory_forgotten(self, limit: int = 3) -> list:
        return self.memory.forgotten_important(limit)

    async def memory_embed(self, texts: list) -> list:
        return await self.memory.embed(texts)

    def memory_restore(self, qv: list, top_k: int = 3) -> list:
        return self.memory.restore(qv, top_k)

    async def mood_event(self, kind: str) -> None:
        """记一次「机器人自己经历的事」（超时、工具失败、被纠正）。

        原来情绪只从用户文本里读，它被超时拖死、被工具坑了、被纠正了都不会有反应，
        只有「听到什么」，没有「经历了什么」。失败也不该影响主流程，所以全吞异常。
        """
        try:
            m = mood.settle(mood.load())
            mood.observe_event(m, kind)
            mood.save(m)
        except Exception as e:  # noqa: BLE001
            log.debug("记录情绪事件失败（%s）：%s", kind, e)

    async def _stream_raw(self, messages: list[dict], tools_arg=None, on_text=None,
                          max_tokens: int | None = None,
                          thinking: bool | None = None) -> dict:
        self._sync_llm_db()
        return await self.llm.stream(messages, tools_arg, on_text,
                                     max_tokens=max_tokens, thinking=thinking)

    def _memory_port(self):
        """记忆契约的入口；`chat.memory_port=false` 时返回 None，走旧路径。

        P1 只读：fact 召回已经迁过去；knowledge / profile 仍在旧路径（P3 再迁）。
        """
        try:
            on = bool((config.load().get("chat") or {}).get("memory_port", True))
        except Exception:  # noqa: BLE001
            on = True
        if not on:
            return None
        if getattr(self, "_mp", None) is None:
            from app_core.memory_port import CoreMemoryPort

            self._mp = CoreMemoryPort(self)
        return self._mp

    def _affect_port(self):
        """情绪契约；`chat.affect_port=false` 或 memory_port 关闭时返回 None，走旧路径。"""
        try:
            cfg = config.load().get("chat") or {}
            on = bool(cfg.get("affect_port", True)) and bool(cfg.get("memory_port", True))
        except Exception:  # noqa: BLE001
            on = True
        if not on:
            return None
        if getattr(self, "_ap", None) is None:
            from app_core.affect_port import CoreAffectPort

            self._ap = CoreAffectPort()
        return self._ap

    def _style_mode(self) -> str:
        """rules = 旧规则；model = 只给人格和例子。未知值按 model。"""
        try:
            mode = str((config.load().get("chat") or {}).get("style_mode", "model")).lower()
        except Exception:
            mode = "model"
        return "rules" if mode == "rules" else "model"

    def _context_format(self) -> str:
        """compact = 单背景块；legacy = 旧的分块注入。未知值按 compact。"""
        try:
            mode = str((config.load().get("chat") or {}).get("context_format", "compact")).lower()
        except Exception:
            mode = "compact"
        return "legacy" if mode == "legacy" else "compact"

    def _thinking_for(self, user_text: str, images: list | None) -> bool | None:
        """这一轮要不要开思考。None = 用默认（开）。

        关思考的代价是「不再纠正错误前提、事实更松」，所以只在**明确闲聊**时关：
        短句、没有问号、没有技术词、不带图。其余一律交给思考模式。
        """
        if not self.thinking_casual:
            return None
        t = (user_text or "").strip()
        # 表情/贴图/短推论：这类轮次先接情绪，不需要先推理再回答。
        if prompt_mod.is_reaction_turn(
                t, len(images or []),
                bool(images) and prompt_mod.image_mode(t) == "fix"):
            return False
        if images:
            # 求助图仍开思考；分享/表情图不开。
            return None if prompt_mod.image_mode(t) == "fix" else False
        # 2026-11-10 阈值从 30 降到 20：实测「dsh 和 cc 都只是一个平台…」「还有什么和
        # harness 差不多价格好用的」这类 26–29 字的句子被当成闲聊关了思考，
        # 结果它既不搜也不推理，直接凭印象答。长句基本都不是纯闲聊。
        if len(t) >= 20:
            return None
        if t.endswith(("？", "?")) or "？" in t or "?" in t:
            return None
        if any(w in t for w in ("为什么", "怎么", "报错", "区别", "方案", "原理",
                                "帮我", "查", "多少", "能不能", "是不是该",
                                "价格", "多少钱", "推荐", "哪个好", "值不值",
                                "合适", "搜", "找")):
            return None
        return False

    async def _run_tools(self, msgs: list[dict], allow_meme: bool = True,
                         images: list[str] | None = None,
                         retry: bool = False,
                         on_text=None,
                         budget: int | None = None,
                         stats: dict | None = None,
                         thinking: bool | None = None,
                         allow_tools: bool = True) -> str:
        """工具循环本体在 tool_loop.py。这里只决定「哪些工具这轮不给用」。

        allow_tools=False 是给外人的：只放行 tools.PUBLIC_TOOLS（查资料、发表情），
        其余一律禁掉。工具层不知道谁在说话，note_list / kb_read 这类只读工具
        读的都是他的笔记和知识库，对别人同样不能开。
        """
        banned = set() if allow_meme else {"meme_send"}
        if not allow_tools:
            allowed = getattr(tools, "PUBLIC_TOOLS", frozenset())
            banned |= {((t.get("function") or {}).get("name") or "") for t in tools.TOOLS
                       if ((t.get("function") or {}).get("name") or "") not in allowed}
        if retry:
            banned |= SIDE_EFFECT_TOOLS
        return await tool_loop.run(
            self, msgs,
            complete=lambda m, t: self.complete_raw(m, t, max_tokens=budget,
                                                    thinking=thinking),
            stream=lambda m, t, cb: self._stream_raw(m, t, cb, max_tokens=budget,
                                                     thinking=thinking),
            tools_mod=tools,
            tools_banned=banned,
            max_rounds=self.max_tool_rounds,
            tool_timeout=self.tool_timeout,
            total_timeout=self.tool_total,
            images=images or [],
            on_text=on_text,
            stats=stats,
        )

    async def reply(self, session: str, user_text: str,
                    images: list | None = None,
                    on_sentence=None,
                    store_text: str | None = None,
                    caps: Capability | None = None,
                    delivery_policy: DeliveryPolicy | None = None) -> tuple[list[str], list[str]]:
        """返回 (分段后的回复, 待发送的图片路径)。

        images：本轮用户发来的图片本地路径。只注入当前这轮，历史里只留文字标记。
        store_text：入库版本。文件正文这类超长输入只在**本轮**给模型看全文，
        历史里存一份压缩摘要，否则每轮都要把 8000 字重新发一遍。
        on_sentence：传了就启用流式，收到完整句子立即回调；返回值为还没发出去的尾巴。

        流水线四段：prepare 取上下文 → compose 拼 prompt → generate 调模型 →
        commit 收尾。拆分不改行为，只是把原来一个 356 行的局部作用域切开。
        """
        turn = await self._turn_prepare(session, user_text, images, store_text)
        await self._turn_compose(turn)
        await self._turn_generate(turn, on_sentence)
        await self._turn_commit(turn, on_sentence)

        def _plan(text: str) -> list[str]:
            policy = delivery_policy or DeliveryPolicy(max_chars=turn.seg_max)
            return plan_delivery(text, policy, caps)

        if on_sentence and turn.streamed:
            # 流式已经发出去的不再返回，只把没说完的尾巴交回去
            tail = _plan(turn.buffer) if turn.buffer.strip() else []
            return tail, turn.ai_images
        return _plan(turn.text), turn.ai_images

    def seg_max(self, m: dict) -> int:
        """情绪决定一条消息多长：累了碎一点，有精神可以连成句。

        分段长度和语气一样是「怎么说」的一部分。累的时候发一大段长句很违和；
        心情好、精力足时又切得太碎会像在蹦字。
        """
        try:
            e = float(m.get("energy", 0.7))
            v = float(m.get("valence", 0.0))
        except Exception:
            return 25
        if e < 0.3:
            return 18
        if e > 0.8 and v > 0.2:
            return 32
        return 25

    def _schedule_shadow(self, user_text: str, reply: str) -> None:
        """影子模式：只记日志，不发消息。用来在真改行为前先量收益。

        - 补一句：生成「本来可以再补的那句」，落盘
        - 正例：他这句是笑/认同就记一条（之前只有纠错，正例全丢了）
        抽样：不是每轮都跑，默认 30%，省 token。
        """
        try:
            shadow.record_positive(user_text, reply)        # 纯本地，无成本
            if not self.shadow_followup:
                return
            if random.random() > self.shadow_sample:
                return
        except Exception as e:  # noqa: BLE001
            log.debug("影子记录失败：%s", e)
            return

        async def _run() -> None:
            try:
                await shadow.record_followup(self.llm, user_text, reply)
            except Exception as e:  # noqa: BLE001
                log.debug("影子补句失败：%s", e)

        asyncio.create_task(_run())

    def _schedule_summary(self, session: str) -> None:
        """把记忆总结丢到后台，不拖慢回复。同一会话不并发总结。"""
        if session in self._summarizing:
            return
        self._summarizing.add(session)

        async def _run() -> None:
            try:
                await self._maybe_summarize(session)
            except Exception as e:  # noqa: BLE001
                log.exception("记忆总结失败（%s）：%s", session, e)
            finally:
                self._summarizing.discard(session)

        asyncio.create_task(_run())

    async def get_digest(self, session: str) -> str:
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                cur = await db.execute(
                    "SELECT digest FROM session_digest WHERE session=?", (session,))
                row = await cur.fetchone()
            return str(row[0] or "") if row else ""
        except Exception:
            return ""

    async def _save_digest(self, session: str, text: str, last_id: int) -> None:
        """写纪要。调用方负责保证 text 已经是最终版本。"""
        async with aiosqlite.connect(DB, timeout=15) as db:
            await db.execute(
                "INSERT OR REPLACE INTO session_digest(session, digest, last_id, updated_ts)"
                " VALUES (?,?,?,?)", (session, text, last_id, time.time()))
            await db.commit()

    async def _update_digest(self, session: str, dialog: str, last_id: int) -> str:
        """单独一次调用重写纪要（兜底路径）。主路径见 _summarize_once。"""
        old = await self.get_digest(session)
        raw = await self.complete_summary(
            [{"role": "system", "content": DIGEST_SYSTEM},
             {"role": "user",
              "content": f"【旧纪要】\n{old or '（还没有）'}\n\n【新对话】\n{dialog}"}],
            max_tokens=600)
        text = (raw or "").strip()
        if len(text) < 20:
            return old
        text = " ".join(text.split())[:DIGEST_MAX]
        try:
            await self._save_digest(session, text, last_id)
        except Exception:
            return old
        return text

    async def _claim(self, name: str, ttl: float = 900) -> bool:
        """抢一把带过期时间的锁。抢到返回 True。

        用数据库而不是内存变量：重启、多实例都不会重复跑。
        """
        # 先试插入；行已存在（锁还在有效期内）就抛 IntegrityError，
        # 再走「过期才允许覆盖」的 UPDATE。注意不能用 UPSERT：
        # INSERT ... ON CONFLICT DO UPDATE ... WHERE 在冲突时 rowcount 仍是 1，
        # 判断不出到底抢没抢到。
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                now = time.time()
                try:
                    await db.execute("INSERT INTO locks(name, ts) VALUES (?,?)", (name, now))
                    await db.commit()
                    return True
                except Exception:
                    cur = await db.execute(
                        "UPDATE locks SET ts=? WHERE name=? AND ts < ?",
                        (now, name, now - ttl))
                    await db.commit()
                    return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            log.warning("抢锁失败（%s）：%s", name, e)
            return False

    async def _release(self, name: str) -> None:
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                await db.execute("DELETE FROM locks WHERE name=?", (name,))
                await db.commit()
        except Exception:
            pass

    async def _maybe_summarize(self, session: str) -> None:
        lock = "summary:" + session
        if not await self._claim(lock):
            return
        try:
            await self._summarize_once(session)
        finally:
            await self._release(lock)

    async def catch_up_summary(self) -> dict:
        """启动时补一次总结，接住「崩溃窗口」里没固化的对话。

        正常路径是每 8 轮触发，或闲置 30 分钟后由下一条消息触发。两者都要求
        **还有下一条消息**。进程如果在这中间被杀（OOM / 手动 kill / 发版重启），
        这几轮对话已经写进 chat.db，但永远等不到总结——用户以为聊过的事它记得，
        其实一条记忆都没落。

        两个门槛防止「每次重启都白烧一次模型」：
        - 至少攒了 CATCHUP_MIN_ROUNDS 轮；
        - 距上次成功总结超过 CATCHUP_MIN_IDLE 秒（正常发版重启几秒就回来了）。
        """
        session = self.private_session()
        if not session:
            return {"ran": False, "reason": "no-admin", "pending": 0}
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE session=? AND role='user'",
                (session,))
            n = int((await cur.fetchone())[0] or 0)
            cur = await db.execute(
                "SELECT last_count, last_id, ts FROM summaries WHERE session=?",
                (session,))
            row = await cur.fetchone()
        if not row:
            return {"ran": False, "reason": "no-state", "pending": n}
        last, last_id, ts = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)
        pending = n - last
        idle = time.time() - ts
        if pending < CATCHUP_MIN_ROUNDS or idle < CATCHUP_MIN_IDLE:
            return {"ran": False, "reason": "fresh", "pending": pending}
        lock = "summary:" + session
        if not await self._claim(lock):
            return {"ran": False, "reason": "busy", "pending": pending}
        try:
            await self._summarize_once(session, force=True)
        finally:
            await self._release(lock)
        return {"ran": True, "reason": "caught-up", "pending": pending}

    async def _check_contradictions(self, doc_id: str, data: dict,
                                    vec: list[float]) -> None:
        """新记忆写完后检查：有没有旧记忆被它推翻。

        为什么值得多一次调用：两条互相矛盾的事实同时留在库里，召回会把它们一起
        塞进 prompt，模型只能猜哪条对——表现出来就是「它记混了」。
        只在相似度够高时问（见 contradict.CAND_MIN_SCORE），大部分总结不触发。
        """
        cfg = (config.load().get("memory") or {})
        mode = str(cfg.get("contradiction_check", "on")).strip().lower()
        if mode == "off" or not vec:
            return
        try:
            cands = self.memory.similar_active(
                vec, contradict.MAX_CAND, contradict.CAND_MIN_SCORE,
                exclude={doc_id})
        except Exception as e:  # noqa: BLE001
            log.debug("矛盾检测取候选失败：%s", e)
            return
        if not cands:
            return
        raw = await self.complete_summary(
            contradict.build_messages(data.get("summary") or "",
                                      data.get("key_facts") or [], cands),
            max_tokens=300, temperature=0.0)
        pairs = contradict.parse(raw)
        if not pairs:
            return
        by_id = {c["doc_id"]: c for c in cands}
        applied = 0
        for p in pairs:
            cand = by_id.get(p.get("old"))
            if cand is None:
                continue            # 模型编了个不存在的编号，忽略
            ok, reason = contradict.should_supersede(p, cand["score"], applied)
            contradict.record(p, cand, ok and mode == "on", reason,
                              new_doc=doc_id, mode=mode)
            if ok and mode == "on":
                if self.memory.supersede(cand["doc_id"], doc_id,
                                         p.get("why") or ""):
                    applied += 1
        if applied:
            log.info("矛盾检测：%d 条旧记忆被新记忆 %s 取代", applied, doc_id[:8])

    async def _summarize_once(self, session: str, force: bool = False) -> None:
        """按消息 id 水位增量固化记忆。

        用 id 而不是消息条数当水位：条数水位在工具调用、分段回复、文件消息
        这些「条数不对称」的场景下会错位，导致内容被重复总结或整段跳过。

        三个边界：
        - 写记忆成功、推水位失败 → 重跑时 range_key 幂等，不会写第二条
        - 推水位成功、写记忆失败 → 不会发生（先写后推，写失败就 return）
        - 同一段反复失败 → 熔断跳过并报错，不让它把后续记忆全堵死
        """
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(MAX(id),0) FROM messages "
                "WHERE session=? AND role='user'", (session,))
            n, max_id = await cur.fetchone()
            cur = await db.execute(
                "SELECT last_count, last_id, ts, fail_count, fail_id "
                "FROM summaries WHERE session=?", (session,))
            row = await cur.fetchone()
        if row:
            last, last_id, last_ts = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)
            fails, fail_id = int(row[3] or 0), int(row[4] or 0)
        else:
            last, last_id, last_ts, fails, fail_id = 0, 0, 0.0, 0, 0
        n, max_id = int(n), int(max_id)
        if row and last_id == 0 and last > 0:
            # 老库迁移：有旧的条数水位但没有 id 水位，对齐到当前最新消息，
            # 避免把已经固化过的历史再总结一遍。
            await self._save_summary_state(session, last, max_id)
            last_id = max_id
        enough_rounds = (n - last) >= SUMMARY_EVERY
        idle_long = (time.time() - last_ts) >= self.summary_idle_seconds
        if not (force or enough_rounds or idle_long):
            return
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT id, role, content FROM messages "
                "WHERE session=? AND id>? ORDER BY id LIMIT ?",
                (session, last_id, SUMMARY_BATCH))
            rows = await cur.fetchall()
        if len(rows) < 4:
            return
        target = int(rows[-1][0])
        dialog = summarize.format_dialog([(r[1], r[2]) for r in rows])
        # 必须用 summary 模型（deepseek-chat）：flash 在长 JSON 上约 1/3 会截断，
        # 截断后 parse 不出 summary，这段对话就静默丢失了。
        # 纪要一起要：同一次调用顺带产出「旧纪要 + 这段对话」的新纪要。
        # 之前是两次独立调用、同一段对话发两遍（7 天里 summary 的输出 token
        # 和聊天本身差不多），合并后少一次完整 prompt。
        old_digest = ""
        try:
            old_digest = await self.get_digest(session)
        except Exception:
            pass
        # 已有主题词表：让模型复用标签，而不是每篇自己造一个近义词
        # （2026-11-05 实测：410 个标签里 393 个只出现过一次，实体连边因此几乎连不起来）。
        # 取不到就不加提示，摘要照常跑。
        try:
            topic_vocab = self.memory.topic_vocab()
        except Exception:  # noqa: BLE001
            topic_vocab = []
        hint = summarize.vocab_hint(topic_vocab)
        sys_prompt = summarize.SYSTEM + ("\n\n" + hint if hint else "")
        raw = await self.complete_summary(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": (
                 f"【旧纪要】\n{old_digest or '（还没有）'}\n\n"
                 f"【这段对话】\n\n{dialog}\n\n整理成一条长期记忆，并更新纪要。")}],
            max_tokens=800)
        data = summarize.parse(raw)
        # 归并到已有词表：模型偶尔还是会给「求职面试」这种自造的近义标签，
        # 而基于实体的连边只认完全相同的字符串。
        if data.get("topics"):
            data["topics"] = vocab_mod.canonicalize(data["topics"], topic_vocab)
        if not data.get("summary"):
            same = (fails + 1) if fail_id == target else 1
            if same >= SUMMARY_MAX_FAILS:
                log.error("总结连续失败 %d 次，跳过 %s 到 id=%s 这一段（会丢这段记忆）",
                          same, session, target)
                await self._save_summary_state(session, n, target)
            else:
                await self._save_fail(session, last, last_id, same, target)
            return
        # 整段对话「解决了没有」：比逐条余弦更全局，折算进情绪
        if data.get("progress"):
            mood.apply_progress(data["progress"])
        try:
            vec = (await self.memory.embed([data["summary"]]))[0]
            payload = {
                "session": session,
                "topics": data.get("topics", []),
                "key_facts": data.get("key_facts", []),
                "vec": vec,
                "followups": data.get("followups", []),
                "range_key": f"{session}:{last_id}-{target}",
            }
            port = self._memory_port()
            if port is not None:
                from app_core.memory_port import MemoryItem

                doc_id = await port.remember(MemoryItem(
                    type="fact", text=data["summary"],
                    confidence=float(data.get("importance", 0.5) or 0.5),
                    source="summary", payload=payload))
            else:
                doc_id = self.memory.add_document(
                    text=data["summary"], session=session,
                    importance=data.get("importance", 0.5),
                    topics=data.get("topics", []),
                    key_facts=data.get("key_facts", []),
                    vec=vec,
                    followups=data.get("followups", []),
                    range_key=f"{session}:{last_id}-{target}")
            # 矛盾检测：新记忆可能让旧记忆过时（「在找工作」→「已入职」）。
            # 只在相似度够高时才多问一次模型；失败不影响写入。
            try:
                await self._check_contradictions(doc_id, data, vec)
            except Exception as e:  # noqa: BLE001
                log.warning("矛盾检测失败：%s", e)
            # followup 单独存一份：记忆会被清理归档，跟进事项不该跟着消失
            if data.get("followups"):
                port_t = self._memory_port()
                if port_t is not None:
                    from app_core.memory_port import MemoryItem

                    for _t in data["followups"]:
                        try:
                            await port_t.remember(MemoryItem(
                                type="task", text=str(_t), source="memory",
                                payload={"kind": "followup", "source": "memory",
                                         "ref": doc_id}))
                        except Exception as e:  # noqa: BLE001
                            log.debug("跟进写入跳过（%s）：%s", type(e).__name__, _t)
                else:
                    followup.add_many(data["followups"], "memory", doc_id)
            # 滚动纪要：历史原文只留最近 2500 字，更早的靠这段纪要接住。
            # 优先用同一次调用带回来的 digest；模型没给（或太短）再退回单独一次调用，
            # 保证旧行为可用，不会因为一次 JSON 缺字段就把纪要停更。
            try:
                new_digest = (data.get("digest") or "").strip()
                if len(new_digest) >= 20:
                    await self._save_digest(session, new_digest[:DIGEST_MAX], target)
                else:
                    await self._update_digest(session, dialog, target)
            except Exception as e:  # noqa: BLE001
                log.warning("更新滚动纪要失败：%s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("写记忆失败，水位不动：%s", e)
            await self._save_fail(session, last, last_id, fails + 1, target)
            return
        await self._save_summary_state(session, n, target)

    async def _save_summary_state(self, session: str, count: int, last_id: int) -> None:
        async with aiosqlite.connect(DB, timeout=15) as db:
            await db.execute(
                "INSERT OR REPLACE INTO summaries"
                "(session, last_count, last_id, ts, fail_count, fail_id) "
                "VALUES (?,?,?,?,0,0)", (session, count, last_id, time.time()))
            await db.commit()

    async def _save_fail(self, session: str, count: int, last_id: int,
                         fails: int, fail_id: int) -> None:
        """只记失败次数，不动水位。ts 保持不变，下一轮还会再试。"""
        async with aiosqlite.connect(DB, timeout=15) as db:
            await db.execute(
                "UPDATE summaries SET fail_count=?, fail_id=? WHERE session=?",
                (fails, fail_id, session))
            await db.execute(
                "INSERT OR IGNORE INTO summaries"
                "(session, last_count, last_id, ts, fail_count, fail_id) "
                "VALUES (?,?,?,?,?,?)",
                (session, count, last_id, 0.0, fails, fail_id))
            await db.commit()

    def reload_persona(self) -> None:
        self.persona = load_persona()
        self.stranger_persona = load_stranger_persona()

    def persona_for(self, session: str) -> dict:
        """本人用他的人格，别人用外人版。两份都不含对方的记忆——人格本身就是上下文。"""
        return self.persona if self._is_owner(session) else self.stranger_persona
