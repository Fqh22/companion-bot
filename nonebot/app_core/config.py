"""运行时配置：读 /opt/nonebot/config.yaml，带默认值。"""
from __future__ import annotations

import copy
import logging
from pathlib import Path

from app_core import paths as _paths

import yaml

log = logging.getLogger("qqbot.config")

CONFIG_PATH = _paths.NB / "config.yaml"

DEFAULTS = {
    "admin": {
        # 管理员 QQ。仓库里是占位 "0"，部署时在云端 config.yaml 填真实号。
        # 留空或 "0" 的后果：记忆不写入、主动消息与定时推送发不出去。
        "user_id": "0",
        # 多入口管理员：QQ 继续用 user_id；console/其他平台登记到这里。
        # 例如 [{"platform": "console", "user_id": "local"}]
        "identities": [],
    },
    "llm": {
        "api": "https://api.deepseek.com/v1/chat/completions",
        # 2026-11-12 A/B：chat 在判断/澄清/承认不知道上明显更稳；编数据两者一样。
        # deepseek-chat 的单价与 flash 同级（见 PRICES），代价是首字慢约 0.5–1 秒。
        "model": "deepseek-chat",
        "temperature": 1.15,
        "max_tokens": 1000,
        # 单次回复最多几轮工具调用。每多一轮就多一次完整 LLM 调用（约 +1.5s）。
        "max_tool_rounds": 6,
        # 一次回复的总时限（秒）。工具 6 轮 × 90s 最坏能拖到 9 分钟，用户不会等。
        "reply_deadline_seconds": 45,
        # 思考模式：开着回复更有内容但更慢更贵，且 temperature 会失效。
        # 2026-10-31 实测后选择开启（首字 +0.8s，每次多约 100 输出 token）。
        "thinking": True,
        # 闲聊轮单独关思考。原来只在 ChatCore 里用 get 兜底，没进 DEFAULTS，
        # 配置面板会把它当成未知键；补进来让「默认值」也是真的。
        # 2026-11-10 用户决定常开思考：清空会话也走思考，不再按「短句/无问号」关。
    # 分流逻辑保留在 _thinking_for 里，把这里改回 True 即可恢复。
    "thinking_casual": False,
    },
    "summary": {
        "model": "deepseek-chat",
        "max_tokens": 700,
        "retry_model": "deepseek-chat",
        # 结构化 JSON 输出不需要思维链，关掉更稳更快，也不会被推理吃掉输出预算
        "thinking": False,
    },
    "embedding": {
        "api": "https://open.bigmodel.cn/api/paas/v4/embeddings",
        "model": "embedding-3",
    },
    "meme": {
        # 按 valence 正负分档；缺省时回落到 mood_threshold（见 ChatCore.__init__）
        # 2026-11-01 重标定：旧值 0.15/0.20/0.18 是按「线性累加」时代定的；
        # 新情绪模型冲量带饱和，一条正面词只到 0.154、两条负面才到 -0.089，
        # 负向 0.18 要连说三条才触发，越改越难发。现在按新曲线重定。
        "threshold_positive": 0.12,
        "threshold_neutral": 0.16,
        "threshold_negative": 0.08,
        "mood_threshold": 0.20,
    },
    "memory": {
        "recall_min_score": 0.35,
        # 时间/重要性调制（详见 memory.recall）。相似度仍是主导。
        "recall_time_tau_days": 30,
        "recall_time_weight": 0.3,
        "recall_importance_weight": 0.1,
        "recall_keep_important": 0.8,
        # 关键词通道（BM25+RRF）目前只跑影子：落盘对比，不改召回结果
        "shadow_retrieval": True,
        # 同一段文字（去空白后完全相同）在这个窗口内不重复写第二条。
        # range_key 只保护带 range 的新记忆，老数据没有 range，只能靠文本兜底。
        "dup_window_minutes": 10,
        "cleanup_days_important": 180,
        "cleanup_days_normal": 90,
        "cleanup_days_low": 30,
        "cleanup_max_active": 3000,
        # 矛盾检测：新记忆写入时，检查有没有旧记忆被它推翻（「在找工作」→「已入职」）。
        # off = 不查；shadow = 只记 data/contradictions.jsonl 不改状态；on = 真的标记取代。
        # 取代是软删，/memory_restore 能捞回来。
        "contradiction_check": "on",
    },
    # 自我画像注入。off=不注入也不记；shadow=只把「本来会注入的那段」落盘；
    # on=真的拼进 prompt。默认 shadow，攒够样本再开。
    "profile": {
        "inject": "shadow",
    },
    "tools": {
        # 单个工具调用的超时；总预算是一轮回复里所有工具调用的上限
        "timeout_seconds": 30,
        "total_timeout_seconds": 120,
    },
    "context": {
        "history_max_chars": 2500,
        "history_max_items": 40,
        "history_item_max_chars": 1500,
        "recall_query_items": 2,
        "summary_idle_minutes": 30,
    },
    # 情绪动力学（详见 mood.py 顶部说明）。都是水龙头，调错了会立刻可感：
    # tau_* 越小 = 忘得越快；energy_burst_cost 越大 = 连发越容易累。
    "mood": {
        "tau_valence_hours": 5.0,
        "tau_base_hours": 72.0,
        "tau_energy_hours": 8.0,
        "tau_social_hours": 6.0,
        "night_drain": 0.06,
        "sleep_recover_hours": 2.0,
        "energy_base_cost": 0.004,
        "energy_burst_cost": 0.016,
    },
    # 输入：现在没有「先等再答」的防抖了（实测合并率 0、白等 1.05s），
    # 文本零等待；只有图片/文件之后等 media_settle_seconds，看他有没有补一句。
    "chat": {
        "media_settle_seconds": 1.5,
        # 影子模式：原来同样只在 ChatCore 里用 get 兜底，没进 DEFAULTS。
        "shadow_followup": True,
        "shadow_sample": 0.3,
        # 引用回复：第一段带「回复你那条」。默认关——用户实测觉得每条都挂引用框太重。
        "quote_reply": False,
        # 记忆契约（docs/10）：P1 起 fact 召回走 MemoryPort；false 可退回旧路径。
        "memory_port": True,
        # 情绪契约（docs/11）：mood 的读/写走 AffectPort；false 可退回旧路径。
        "affect_port": True,
        # 分析类问题先搜一轮（给材料，防止拿印象编数据）；false 关掉。
        "force_search": True,
        # rules = 旧的长度/形式/反应规则；model = 只给人格和例子，让模型自己判断。
        # 回退时把它改成 rules 并重启，不需要回滚代码。
        "style_mode": "model",
        # compact = 记忆/知识/技能/纠正/待办合成一个【背景】块并限预算；
        # legacy = 旧的分块注入。回退只改这里。
        "context_format": "compact",
    },
    # 群聊：独立链路（见 app_core/group.py）。默认关闭，白名单为空 = 空跑。
    "group": {
        "enabled": False,
        "whitelist": [],             # 群号列表；空 = 任何群都不回
        "context_turns": 20,         # 进程内短期上下文条数（重启即忘）
        "rate_per_minute": 3,        # 每群每分钟最多回几条
        "cooldown_seconds": 5,       # 两次回复的最小间隔
        "daily_token_budget": 50000, # 每群每天 token 上限，超了静默停
        "reply_max_tokens": 300,     # 群里说短句
    },
    # 话题巡检：每周日 20:30 主动查一次会变的事，只报变化（见 app_core/watch.py）
    "watch": {
        # 只在 watchlist.json 不存在时用来初始化；之后直接改那个文件即可
        "topics": [],
    },
    # 图片：长边与 JPEG 质量。1600 是为了让手机截图里的字还读得清
    # （1024 时 13px 正文只剩约 5px，报错码和路径会被读错）。
    "image": {
        "max_side": 1600,
        "quality": 90,
    },
    "pricing": {
        # 按 DeepSeek 官网高峰价保守估算（空闲时段约半价）。
        # 注意 deepseek-chat 目前实际路由到 deepseek-flash。
        # cache_in 是「上下文缓存命中」的输入单价，约为未命中的 1/4
        "deepseek-flash": {"in": 2.0, "out": 8.0, "cache_in": 0.5},
        "deepseek-chat": {"in": 2.0, "out": 8.0, "cache_in": 0.5},
        "cache_in": 0.5,
    },
}

_cache: dict | None = None
# 上一次读 config.yaml 的错误（空 = 正常）。YAML 写错时**不能静默回落到默认值**：
# 那会把 admin.user_id、模型、所有阈值一起换掉，表现只是「它变笨了 / 不找我了」。
_load_error = ""
# 读到的是哪个版本的文件。进程内是缓存，改文件不重启不会生效——
# 这件事以前完全不可见，表现是「我明明改了阈值，怎么没变」。
_loaded_mtime = 0.0


def load_error() -> str:
    """上一次解析 config.yaml 的错误，给启动自检（/ping）用。"""
    return _load_error


def owner_session() -> str:
    """主人私聊的 session key；admin 未配置时返回空串。

    为什么集中在这里：日记 / 原文归档 / 周报指标 / 主动消息都要判断
    「这条消息是不是他的」。这个判断只能有一个来源——各模块自己 parse
    admin.user_id，迟早有一处漏掉，表现是陌生人的话混进他的日记。
    """
    admin = str((load().get("admin") or {}).get("user_id") or "").strip()
    if not admin or admin == "0":
        return ""
    return f"private:{admin}"


def stale() -> bool:
    """config.yaml 比进程里加载的那份新 —— 改了但没重启。

    没加载过就先加载一次：否则 _loaded_mtime 是 0，任何文件都会被判成「过期」。
    """
    try:
        if _cache is None:
            load()
        return CONFIG_PATH.stat().st_mtime > _loaded_mtime + 0.001
    except Exception:  # noqa: BLE001
        return False


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> dict:
    global _cache, _load_error, _loaded_mtime
    if _cache is not None and not force:
        return _cache
    data = {}
    # 每次加载都清空错误：文件被删掉/换回来之后，"上次解析失败"不该一直挂着
    _load_error = ""
    try:
        _loaded_mtime = CONFIG_PATH.stat().st_mtime
    except Exception:  # noqa: BLE001
        _loaded_mtime = 0.0
    if CONFIG_PATH.exists():
        try:
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            # 缩进写错、tab 混用都会走到这里。**必须喊出来**，
            # 否则整份配置悄悄变回 DEFAULTS，线上只看得到「它不太对劲」。
            _load_error = f"{type(e).__name__}: {e}"
            log.error("[config] %s 解析失败，全部回落默认值：%s", CONFIG_PATH, _load_error)
            data = {}
    _cache = _merge(DEFAULTS, data)
    return _cache
