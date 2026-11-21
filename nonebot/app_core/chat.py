"""[orchestration] chat 核心：人格、LLM、会话、记忆、情绪、知识库、工具循环、分段。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app_core import paths as _paths

import aiosqlite
import httpx

from app_core import (alerts, attention, config, contradict, files, followup, intent, kb,
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


from app_core.history import HistoryMixin, prompt_blocks
from app_core.chat_reply import ReplyMixin  # noqa: F401  兼容老调用方
from app_core.summary import SummaryMixin
from app_core.chat_turn import ChatTurnMixin, Turn


def _lock_owner() -> str:
    """锁的持有者标识：pid@host。用于 release 时只删自己的锁。"""
    import os
    import socket

    return f"{os.getpid()}@{socket.gethostname()}"


class ChatCore(HistoryMixin, SummaryMixin, ReplyMixin, ChatTurnMixin):
    """聊天编排器。**插件只许调下面列出的成员**（ops/lint-plugins.py 会查）。

    公共接口：
        reply / reply_deadline / last_prompts   一轮回复 + 截止时间 + 上轮 prompt
        mood_event                              记一次「机器人自己经历的事」
        usage_stats / config_issues / config_status
        persona / persona_for / reload_persona / private_session / is_owner
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
        chat_cfg = cfg.get("chat") or {}
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
        # 总时限只是「永远不结束」的兜底，不是体验目标：45s 原本是从
        # 「工具 6 轮最坏 9 分钟」倒推的，反过来把长回复/多段语音切成半截。
        # 现在放宽，另设无进展超时（runner.py 的 stall_seconds）。
        self.reply_deadline = float(llm.get("reply_deadline_seconds", 90))
        self.reply_stall_seconds = float(llm.get("reply_stall_seconds", 30))
        # 首字预算：取证阶段的上限。用户感知的是首字，不是整轮。
        self.first_token_budget = float(llm.get("first_token_budget_seconds", 10))
        # 工具预算从**首字预算**派生，不再被总时限绑架：总闸放宽到 90s 后，
        # 若仍按 deadline/3 算，单工具会从 15s 悄悄涨到 30s，取证阶段反而变慢。
        # 旧配置（45 / 10）下这两式数值恰好等于旧值 40s / 15s——行为不变，
        # 但从此改总时限不会连带改工具预算。
        self.tool_total = min(float(tool_cfg.get("total_timeout_seconds", TOOL_TOTAL)),
                              max(10.0, self.first_token_budget + 30))
        self.tool_timeout = min(float(tool_cfg.get("timeout_seconds", TOOL_TIMEOUT)),
                                max(5.0, self.first_token_budget * 1.5))
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
        # 平台自己的主人身份：飞书没有 QQ 号可共用时，会话是 feishu:<open_id>。
        # 只在配置里明确写了 owner 才算可信身份；空配置不会进这个集合。
        self.platform_owner_sessions: set[str] = set()
        _fe = cfg.get("feishu") or {}
        _fe_owner = str(_fe.get("owner_open_id") or "").strip()
        if _fe.get("enabled") and _fe_owner:
            self.platform_owner_sessions.add(f"feishu:{_fe_owner}")

        # 插件门面：命名空间对象（core.learn / core.audit / core.kb …），
        # 见 app_core/plugin_api.py；CORE_API 按两层登记检查。
        from app_core import plugin_api

        for _ns, _mod in plugin_api.build().items():
            setattr(self, _ns, _mod)
        # 只读 MCP：把外部 server 的工具接给模型（默认关；白名单 + 超时 + 不可信隔离）
        try:
            from app_core import mcp as mcp_mod
            from app_core import tools as tools_mod
            mcp_mod.register(tools_mod)
        except Exception as e:  # noqa: BLE001
            log.debug("MCP 注册失败：%s", e)

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
                "prompt_tokens INTEGER, completion_tokens INTEGER, "
                "retry_429 INTEGER DEFAULT 0)")
            # last_id：已固化到的消息水位。老库没有这一列，补上（默认 0 = 从头）
            # fail_count / fail_id：同一段连续总结失败次数，用来熔断毒批次
            for ddl in ("ALTER TABLE summaries ADD COLUMN last_id INTEGER DEFAULT 0",
                        "ALTER TABLE summaries ADD COLUMN fail_count INTEGER DEFAULT 0",
                        "ALTER TABLE summaries ADD COLUMN fail_id INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN latency_ms INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN cache_hit INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN cache_miss INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN retry_429 INTEGER DEFAULT 0",
                        "ALTER TABLE usage ADD COLUMN purpose TEXT DEFAULT ''"):
                try:
                    await db.execute(ddl)
                except sqlite3.OperationalError as e:
                    # 「列已存在」是正常迁移；其它（库锁/磁盘/语法）必须留痕，
                    # 否则 usage 表会永远缺列，直到 /usage 崩了才发现。
                    if "duplicate column" not in str(e).lower():
                        log.warning("chat.db 迁移失败：%s（%s）", ddl, e)
            # /usage 按时间窗口聚合，之前没有索引是整表扫（EXPLAIN 里是 SCAN usage）
            await db.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)")
            # 跨进程/跨重启的任务锁：抢到才执行，避免重启重跑、多实例重复
            await db.execute(
                "CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, ts REAL NOT NULL)")
            try:
                # 持有者：过期锁被别的进程抢走后，原持有者 release 不能删别人的锁
                await db.execute("ALTER TABLE locks ADD COLUMN owner TEXT")
            except Exception:  # noqa: BLE001
                pass
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
                "sha TEXT, chars INTEGER, flags TEXT, body TEXT, blocks TEXT)")
            try:
                await db.execute("ALTER TABLE prompt_log ADD COLUMN body TEXT")
            except Exception:  # noqa: BLE001
                pass          # 已经有了
            try:
                # 分块记账：每块各占多少字符。不记这个，就没人答得上
                # 「现在一轮 prompt 里各块各占多少」——膨胀只能靠感觉。
                await db.execute("ALTER TABLE prompt_log ADD COLUMN blocks TEXT")
            except Exception:  # noqa: BLE001
                pass          # 已经有了
            await db.commit()
        self.memory.init()
        kb.init()
        remind.init()
        followup.init()

    async def caption_images(self, paths: list, limit: int = 2) -> str:
        """给图片生成一句图注，用于写进历史。

        为什么要做：历史里原来只有「（附了 1 张图）」，隔天问「昨天那张图」它完全不记得。
        图注让"图"变成可检索的文字，代价是每张图多一次很小的模型调用（后台跑，不挡回复）。
        """
        from app_core.files import to_data_url
        from app_core import ocr as ocr_mod

        # OCR 先跑：图里的字由专门接口提，比让视觉模型「顺带读」准得多
        # （实测截图 3 行报错：视觉只读到 2-3 行，OCR 全读对）。
        # 失败返回空串 → 退回「视觉自己读」，不影响图注本身。
        # 2026-11-15 踩过的坑：这里只加了下面对 ocr_text 的拼接、没加这个赋值，
        # NameError 被 except 吞掉，图注直接变空（CI 报「图注生成走通」失败）。
        ocr_text = ""
        if paths:
            try:
                ocr_text = await ocr_mod.ocr_text(paths[0])
            except Exception:  # noqa: BLE001
                ocr_text = ""

        # 2026-11-15：图注的目标是「隔天能检索」，不是「完整理解」。
        # 当轮模型直接看原图，不需要图注；图注只进历史、占 token，所以按类型覆盖：
        # 先判类型（截图/表情包/照片/商品），再逼它回答「主体是谁/什么」——
        # 上一版只问「什么场景」，模型会把「蓝发女仆」当成完整答案（其实是 IP 二创）。
        # 但加「别编名字」：IP 猜错会写进历史，变成隔天检索到的假事实。
        content: list[dict] = [{
            "type": "text",
            "text": "先照抄图里最关键的 2-3 行文字（报错、标题、按钮、聊天内容）；"
                    "然后说清三件事，每件不超过一行："
                    "① 这是什么（截图/表情包/照片/商品/软件界面/其他）；"
                    "② 主体是谁或什么（人、角色、物件；知名 IP 就直接说名字）；"
                    "③ 在做什么、什么场景。"
                    "认不出具体是谁就写特征，别编名字。总共 100 字内，只描述不评论。"}]
        for p in (paths or [])[:limit]:
            url = to_data_url(p)
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})
        if len(content) == 1:
            return ""
        try:
            # 图注是附加信息，不能吃掉整轮的 45 秒预算：它在主生成之前被同步 await，
            # 视觉模型一慢（或 429 重试）就把回复时间全占掉，用户只等到「有点卡」。
            msg = await asyncio.wait_for(
                self.complete_raw([{"role": "user", "content": content}],
                                  None, max_tokens=140, thinking=False),
                timeout=8)
            return (msg.get("content") or "").strip()[:200]
        except Exception as e:  # noqa: BLE001
            log.debug("生成图注失败：%s", e)
            return ""

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
                       context_format: str = "legacy",
                       gap_hours: float | None = None,
                       budget_mode: str | None = None) -> list[dict]:
        """拼 prompt：规则在 prompt.py，这里只负责把人格传进去。"""
        if budget_mode is None:
            # 默认 legacy：prompt 快照基线逐字节不变，随时可一键回退。
            # 冻结/离线测试里 config 可能读不到，这时也必须走 legacy，不能崩。
            try:
                budget_mode = str((config.load().get("chat") or {}).get(
                    "budget_mode", "legacy"))
            except Exception:  # noqa: BLE001
                budget_mode = "legacy"
        return prompt_mod.build_messages(
            persona or self.persona, history, user_text, memories, mood_text, knowledge,
            skills, images, extra_system, digest, DIGEST_MAX, lessons, open_items,
            profile, style_mode, context_format, gap_hours, budget_mode)

    async def complete_raw(self, *a, **kw) -> dict:
        """LLM 调用本体在 llm.py，这里只是让 ChatCore 保持原接口。"""
        self._sync_llm_db()
        return await self.llm.complete(*a, **kw)

    async def complete(self, messages: list[dict], max_tokens: int | None = None) -> str:
        msg = await self.complete_raw(messages, None, max_tokens)
        return (msg.get("content") or "").strip()

    async def complete_summary(self, messages: list[dict], max_tokens: int | None = None,
                               temperature: float | None = None,
                               json_mode: bool = False) -> str:
        self._sync_llm_db()
        return await self.llm.summarize(messages, max_tokens, temperature=temperature,
                                        json_mode=json_mode)

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

    def _memory_port(self):
        """记忆契约的入口；`chat.memory_port=false` 时返回 None，走旧路径。

        读已覆盖 fact / knowledge / profile / task（mood 未实现，调用即显式报错）；
        写入接 fact / knowledge / task，画像走 remember_profile。旧注释里
        「P1 只读、knowledge/profile 仍走旧路径」已被 fdf8199 之后的提交追平。
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
                    await db.execute("INSERT INTO locks(name, ts, owner) VALUES (?,?,?)",
                                     (name, now, _lock_owner()))
                    await db.commit()
                    return True
                except Exception:
                    cur = await db.execute(
                        "UPDATE locks SET ts=?, owner=? WHERE name=? AND ts < ?",
                        (now, _lock_owner(), name, now - ttl))
                    await db.commit()
                    return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            log.warning("抢锁失败（%s）：%s", name, e)
            return False

    async def _release(self, name: str) -> None:
        """只删自己的锁。

        原来无条件 DELETE：锁过期被别的进程抢走后，原持有者的 release
        会把别人的锁删掉，两个实例同时跑同一任务。
        """
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                await db.execute(
                    "DELETE FROM locks WHERE name=? AND (owner IS NULL OR owner=?)",
                    (name, _lock_owner()))
                await db.commit()
        except Exception:
            pass

    def reload_persona(self) -> None:
        self.persona = load_persona()
        self.stranger_persona = load_stranger_persona()

    def persona_for(self, session: str) -> dict:
        """本人用他的人格，别人用外人版。两份都不含对方的记忆——人格本身就是上下文。"""
        return self.persona if self._is_owner(session) else self.stranger_persona
