"""[orchestration] 一轮对话的数据结构：Stage/Slot/Turn，以及阶段常量。

从 chat_turn.py 拆出来的原因：Turn 是跨阶段共享的数据契约（chat.py、插件、
测试都 import），和 ChatTurnMixin 的 900 行流程分开后，改数据结构不必通读流程。
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field

from app_core import config

log = logging.getLogger("qqbot.chat")


def _chat():
    return sys.modules["app_core.chat"]


def _topic_overlap(a: str, b: str) -> bool:
    """两句有没有共同的具体词。

    待办只在「这轮真的聊到」时注入；泛泛的短句（「算了」「随便问问」）
    不该触发旧账。宁可漏提，也别主动翻。
    """
    import re

    _STOP = {"我们", "这个", "那个", "就是", "可以", "没有", "什么", "怎么",
             "还是", "但是", "因为", "所以", "如果", "已经", "现在", "时候"}

    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s or "")
        out = {m.lower() for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", s)}
        for run in re.findall(r"[\u4e00-\u9fa5]+", s):
            for i in range(len(run) - 1):
                g = run[i:i + 2]
                if g not in _STOP:
                    out.add(g)
        return out

    return bool(grams(a) & grams(b))


# ---- 注入块的静态槽位表：顺序的唯一真源 ----
# 改造前是 12 处 extra_system.append，顺序 = 追加时序；现在顺序 = 这张表。
# source_stage 说明「数据从哪个阶段来」，不是运行时的 turn.stage。
PREFETCH = "prefetch"
READ_HISTORY = "read_history"
RENDER_SLOTS = "render_slots"
GENERATE = "generate"
POST_WRITE = "post_write"


class StageError(RuntimeError):
    """在错的阶段写槽位：结构上不允许，不是风格问题。"""


@dataclass(frozen=True)
class Slot:
    sid: int
    key: str
    nullable: bool
    source_stage: str


SLOTS: tuple[Slot, ...] = (
    Slot(10, "weather", True, PREFETCH),
    Slot(20, "search", True, PREFETCH),
    Slot(30, "followup", True, READ_HISTORY),
    Slot(40, "style", True, RENDER_SLOTS),
    Slot(50, "reaction", True, RENDER_SLOTS),
    Slot(60, "caption", True, RENDER_SLOTS),
    Slot(70, "share", True, RENDER_SLOTS),
    Slot(80, "situation", True, RENDER_SLOTS),
    Slot(90, "relation", True, RENDER_SLOTS),
)


@dataclass
class Turn:
    """一轮回复的状态。

    拆 reply 的目的：原来 15 个阶段挤在同一个 356 行的局部作用域里，
    改任何一段都得读懂全部。现在阶段之间只传这一个对象，
    哪些是「本轮的」，一眼能看全。
    """
    session: str
    user_text: str
    images: list
    store_text: str | None
    owner: bool
    rid: str
    t0: float
    degraded: int = 0
    m: dict = field(default_factory=dict)
    stored: str = ""
    slots: dict = field(default_factory=dict)
    prefetch: dict = field(default_factory=dict)
    followup_control_reply: str | None = None
    calls: dict = field(default_factory=dict)   # 每个 fetch 被调用几次（判 no_call / degraded）
    caption_text: str = ""                      # RENDER_SLOTS 生成、POST_WRITE 写回
    gap_basis: float = 0.0                      # 上一条用户消息时间（READ_HISTORY 冻结）
    prepare_ms: float = 0.0                     # 取证+组装耗时（首字预算的实际占用）
    first_token_at: float = 0.0                 # 首字时间戳（perf_counter）
    prepare_over_budget: bool = False
    fetch_metrics: dict = field(default_factory=dict)   # 各路取数结果：ok/timeout/error/cancelled
    budget_exhausted: bool = False
    parallel: bool = False
    stage: str = PREFETCH
    digest: str = ""
    history: list = field(default_factory=list)
    query: str = ""
    user_msg_id: int = 0
    quote_text: str = ""
    failed_parts: list = field(default_factory=list)
    mvec: list = field(default_factory=list)
    qv: list | None = None
    skip_recall: bool = False
    seg_max: int = 0
    memories: list = field(default_factory=list)
    knowledge: str = ""
    skills: list = field(default_factory=list)
    lessons: str = ""
    profile_block: str = ""
    open_items: str = ""
    msgs: list = field(default_factory=list)
    allow_meme: bool = True
    ai_images: list = field(default_factory=list)
    buffer: str = ""
    held: str = ""
    streamed: bool = False
    sent_parts: list = field(default_factory=list)
    text: str = ""
    img_budget: int = 0
    task_stats: dict = field(default_factory=dict)
    think: bool = False
    memory_trace: list = field(default_factory=list)
    share_tier: int = -1
    self_check: str = ""
    meta_leak: bool = False
    meta_leak_partial: bool = False   # 元叙述泄漏时已经发了一半（后半段丢了）
    injected_insights: list = field(default_factory=list)   # 本轮注入的洞察（用户否定时要标掉）
    gap: float = 0.0

    def set_slot(self, key: str, text: str | None, order: int | None = None) -> None:
        """写一个注入块。只允许在 RENDER_SLOTS 阶段——这是结构约束，不是注释。

        同槽多块是**白名单能力**，不是默认能力：不传 order 时第二次写同一个槽直接报错，
        逼调用方显式声明优先级。这样「谁先跑完谁排前面」这种调用序依赖无处滋生——
        S2 把两块拆进不同 fetch 之后，顺序也不会漂。

        followup：done=0 / confirm=1 / drop=2。
        """
        if self.stage != RENDER_SLOTS:
            raise StageError(f"槽位 {key} 只能在 {RENDER_SLOTS} 写，当前 {self.stage}")
        # 只有 SLOTS 里的 key 会被 extra_system 渲染：拼错的槽位原先被静默丢弃，
        # 而 lint / slot-contract 都只遍历 SLOTS 自身，CI 不会红。这里显式报错。
        if key not in {s.key for s in SLOTS}:
            raise StageError(f"未知槽位 {key}：只有 SLOTS 里的 key 会被渲染")
        if text is None:
            return
        box = self.slots.setdefault(key, [])
        if order is None:
            if box:
                raise StageError(f"槽位 {key} 已写过；同槽多块必须显式传 order")
            box.append((0, 0, text))
        else:
            box.append((int(order), len(box), text))

    @property
    def extra_system(self) -> tuple[str, ...]:
        """按 SLOTS 的 sid 排槽，槽内按 order 排块——与赋值时序无关。"""
        out: list[str] = []
        for s in sorted(SLOTS, key=lambda x: x.sid):
            for _o, _i, t in sorted(self.slots.get(s.key) or [],
                                    key=lambda x: (x[0], x[1])):
                out.append(t)
        return tuple(out)


def _share_tier_on() -> bool:
    """分享轮档位是否开启。默认关：先跑 ops/replay-share.py 对比再开。"""
    try:
        return bool((config.load().get("chat") or {}).get("share_tier", False))
    except Exception:  # noqa: BLE001
        return False


GEN_RESERVE = 15.0                       # 给生成和发送留的秒数
# 预搜索换全家桶（模型侧检索）后变慢：5 秒是老的自建搜索的预算
_PATH_TIMEOUT = {"weather": 10.0, "search": 25.0}
