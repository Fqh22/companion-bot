"""[orchestration] 会话历史：读写 chat.db 的 messages / session_digest 之外的那部分。

从 chat.py 拆出来的原因：这一组只依赖「库 + 时间 + 管理员身份」，是纯粹的存储层，
和人格、LLM、回复编排无关；Mixin 组合：ChatCore(HistoryMixin, SummaryMixin, ChatTurnMixin)。

DB 仍是模块级常量（与 chat.py 同源）：测试要隔离数据根，补本模块的 DB。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiosqlite

from app_core import paths as _paths
from app_core import prompt as prompt_mod

log = logging.getLogger("qqbot.chat")

DB = _paths.DATA / "chat.db"
_clip = prompt_mod.clip


def prompt_blocks(msgs: list[dict]) -> list[dict]:
    """把一份 messages 拆成「块账」：每块是什么、占多少字符。

    从 chat.py 搬来（只被 _log_prompt 用）：块边界就是 message 边界——
    prompt.py 里每个注入块都是一条独立 system。label 取首行前 16 字。
    """
    out: list[dict] = []
    for i, m in enumerate(msgs or []):
        c = m.get("content")
        if isinstance(c, str):
            chars = len(c)
            label = " ".join(c.split())[:16]
        else:
            chars = len(json.dumps(c, ensure_ascii=False))
            label = "[多模态]"
        out.append({"i": i, "role": str(m.get("role") or ""),
                    "chars": chars, "label": label or "(空)"})
    return out


class HistoryMixin:

    async def history(self, session: str, max_chars: int | None = None,
                      max_items: int | None = None) -> list[dict]:
        """从最近往前取，按字符预算截断（至少保留最近 2 条）。"""
        budget = max_chars or self.history_max_chars
        limit = max_items or self.history_max_items
        async with aiosqlite.connect(DB, timeout=15) as db:
            cur = await db.execute(
                "SELECT role, content, ts FROM messages "
                "WHERE session=? ORDER BY id DESC LIMIT ?",
                (session, limit))
            rows = await cur.fetchall()
        picked, used = [], 0
        for role, content, ts in rows:
            # 先按单条上限压缩，再计入预算：否则一条超长消息会把整段预算吃光
            shown = _clip(content, self.history_item_max)
            c = len(shown)
            if picked and used + c > budget:
                break
            # ts 跟着走：prompt 要用**每条消息自己的时间**标日期 / 换算相对日期
            # （2026-11-22）。库里存的仍是原话，日期只在送给模型的那份副本里。
            picked.append({"role": role, "content": shown, "ts": float(ts or 0)})
            used += c
        return list(reversed(picked))

    async def message_ts(self, row_id: int) -> float:
        """某条消息的落库时间戳；拿不到返回 0。

        即时记忆（memory_instant）写库时要把「我下周三面试」换算成绝对日期，
        基准必须是**这条消息的时间**，不是写记忆那一刻——两者跨午夜时差一天。
        """
        if not row_id:
            return 0.0
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                cur = await db.execute(
                    "SELECT ts FROM messages WHERE id=?", (int(row_id),))
                row = await cur.fetchone()
        except Exception:  # noqa: BLE001
            return 0.0
        return float((row or [0])[0] or 0)

    async def last_user_ts(self, session: str) -> float:
        """上一条用户消息的时间戳。本轮消息还没写，所以不用 before_id 排除。

        reason：gap 原来依赖「本轮刚写入的 id」，导致写库必须发生在 prepare。
        改成读「上一条的时间」之后，写库才能整体挪到 POST_WRITE。
        """
        try:
            async with aiosqlite.connect(DB, timeout=15) as db:
                cur = await db.execute(
                    "SELECT MAX(ts) FROM messages WHERE session=? AND role='user'",
                    (session,))
                row = await cur.fetchone()
        except Exception:  # noqa: BLE001
            return 0.0
        return float((row or [0])[0] or 0)

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

        **没配可信身份时不是本人**。以前这里 return True（"总比哑掉好"），
        代价是配置被清空或新环境漏配时，任意会话都能拿到私有记忆和写工具——
        权限边界不能靠"应该配了"来保证。现在缺省退成普通对话：
        仍能聊，但没有主人记忆、没有写工具、不发主动消息；启动自检会报配置缺失。
        """
        ids = ([self.admin_uid] if self.admin_uid else []) + list(self.admin_ids)
        if ids and any(session == f"private:{uid}" for uid in ids):
            return True
        # 其他平台没有 QQ 号可共用时，用平台自己的主人会话
        # （如 feishu.owner_open_id → "feishu:<open_id>"）。
        # 这些是配置里明确写下的可信身份，不是"空配置默认放行"。
        return session in getattr(self, "platform_owner_sessions", set())

    def is_owner(self, session: str) -> bool:
        """公共只读入口：插件判断会话身份用这个，不直接碰 _is_owner。"""
        return self._is_owner(session)

    def private_session(self) -> str:
        """主人会话的唯一解析入口（与 config.owner_session 口径一致）。

        QQ / admin.identities 优先；只配飞书主人时退回平台会话
        feishu:<open_id>。都没有时返回空串，调用方必须把它当成「没有可信
        主人会话」而不是「查所有用户」。
        """
        uid = self.admin_uid or (self.admin_ids[0] if self.admin_ids else "")
        if uid:
            return f"private:{uid}"
        return getattr(self, "platform_owner_session", "") or ""

    def note_followup_asked(self, text: str, msg_id: int = 0) -> None:
        """实际发出的助手文本如果在问某条跟进，和主动消息共用提问记录。"""
        try:
            from app_core import followup
            followup.note_asked(text, msg_id=msg_id)
        except Exception:  # noqa: BLE001
            return

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
                cur = await db.execute(
                    "SELECT content FROM messages WHERE id=?", (int(row_id),))
                row = await cur.fetchone()
                if row is None:
                    return False
                # 幂等：图片轮已经同步补过，后台任务再补一次不该出现两个「｜图注」
                if suffix in (row[0] or ""):
                    return False
                await db.execute(
                    "UPDATE messages SET content = content || ? WHERE id = ?",
                    (suffix, int(row_id)))
                await db.commit()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("补写消息失败：%s", e)
            return False

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
            # 分块账：和正文一起落，事后才答得上「哪块在长」。
            blocks = json.dumps(prompt_blocks(msgs), ensure_ascii=False)
            async with aiosqlite.connect(DB, timeout=15) as db:
                await db.execute(
                    "INSERT INTO prompt_log(ts,session,sha,chars,flags,body,blocks) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (time.time(), session, hashlib.sha256(blob).hexdigest()[:16],
                     # chars 记**字符数**，与 prompt_blocks / `/prompt_last` 的展示同口径。
                     # 原来存 len(blob)（UTF-8 字节数），中文下约为字符数的 3 倍，
                     # 同一行「字符」与「分块合计」永远对不上。
                     len(blob.decode("utf-8", errors="replace")), tag, body, blocks))
                # 只留最近 100 条：正文占空间，排障也只看最近的
                await db.execute(
                    "DELETE FROM prompt_log WHERE id < "
                    "(SELECT COALESCE(MAX(id), 0) - 100 FROM prompt_log)")
                await db.commit()
        except Exception as e:  # noqa: BLE001
            log.debug("prompt 快照失败：%s", e)
