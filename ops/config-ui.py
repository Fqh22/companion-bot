#!/usr/bin/env python3
"""本地配置面板：只绑 127.0.0.1，通过 SSH 隧道打开。

为什么不直接改 config.yaml：
- 配置文件里大量注释就是文档，PyYAML 存一次会把注释和顺序全丢掉；
  这里用 ruamel.yaml 的 round-trip 模式，只改值，保留注释/顺序/引号风格。
- 保存前校验 + 备份 + diff；非法值不落盘。
- 不做公网后台、不碰 keys.json、不执行任意命令。

用法：
    python ops/config-ui.py --port 8110
    # 浏览器：ssh -L 8110:127.0.0.1:8110 root@<server>
    # 打开启动日志里带 token 的 URL
"""
from __future__ import annotations

import argparse
import difflib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
# 仓库布局：<repo>/nonebot/app_core；部署布局：/opt/nonebot/app_core
NB = ROOT / "nonebot" if (ROOT / "nonebot" / "app_core").is_dir() else ROOT
sys.path.insert(0, str(NB))
try:
    from app_core import config as _appconfig
except Exception as e:  # pragma: no cover - 部署环境缺依赖时才走到
    raise SystemExit(f"读不到 app_core.config（{NB}）：{e}")

DEFAULTS = _appconfig.DEFAULTS
DEFAULT_CONFIG = Path(_appconfig.CONFIG_PATH)

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
except Exception as e:  # pragma: no cover
    raise SystemExit(
        f"需要 ruamel.yaml：pip install -r ops/requirements-config-ui.txt（{e}）")

STATIC = Path(__file__).resolve().with_name("config-ui")
log = logging.getLogger("config-ui")

MAX_BODY = 2 * 1024 * 1024
COOKIE = "cui"

# ---------------------------------------------------------------- schema ----
GROUP_META = {
    "admin": ("管理员", "主人身份；留空会让所有人被当成本人。"),
    "llm": ("对话模型", "回复用的模型、温度、工具轮数与 45 秒硬线。"),
    "summary": ("总结模型", "记忆总结 / 复盘 / 周报 / 日记；长 JSON 建议用 chat。"),
    "embedding": ("向量模型", "记忆召回用的 embedding 服务。"),
    "tools": ("工具", "单次工具调用与整轮工具预算。"),
    "context": ("上下文", "历史预算、召回拼接与空闲总结。"),
    "chat": ("聊天", "媒体等待与影子模式。"),
    "group": ("群聊", "独立链路，默认关闭；白名单为空时谁都不回。"),
    "watch": ("话题巡检", "每周日主动搜索一次，只报真的变了的。"),
    "image": ("图片", "压缩长边与 JPEG 质量。"),
    "mood": ("情绪", "衰减与精力参数，改完要重标定下游阈值。"),
    "meme": ("表情包", "三档触发阈值，按心情正负分。"),
    "memory": ("记忆", "去重、分层清理、召回与矛盾检测。"),
    "profile": ("画像", "自我画像注入：off / shadow / on。"),
    "pricing": ("计费", "每百万 token 单价，仅用于 /usage 估算。"),
}

# id -> (label, description, extra)
F = {
    "admin.user_id": ("管理员 QQ", "你的 QQ 号。留空或 0 会让所有人被当成本人；启动自检会报警。", {"required": False}),
    "llm.api": ("对话 API", "OpenAI 兼容的 chat completions 地址。", {}),
    "llm.model": ("对话模型", "默认 deepseek-flash。", {}),
    "llm.temperature": ("温度", "0–2；思考模式下不生效，切回非思考时恢复。", {"min": 0, "max": 2, "step": 0.05}),
    "llm.max_tokens": ("单条回复上限", "兜底上限；图片/工具轮会再给到 1600。", {"min": 100, "max": 8000}),
    "llm.max_tool_rounds": ("工具轮数", "每多一轮约 +1.5 秒；6 轮通常够用。", {"min": 1, "max": 12}),
    "llm.reply_deadline_seconds": ("回复硬线（秒）", "超时补发已生成部分，不让用户干等。", {"min": 5, "max": 300}),
    "llm.thinking": ("思考模式", "更具体但首字慢约 0.8s，且温度失效。", {}),
    "llm.thinking_casual": ("闲聊关思考", "短句、无问号、无技术词、不带图的轮次关掉思考。", {}),
    "summary.model": ("总结模型", "长 JSON 输出用 chat 更稳。", {}),
    "summary.max_tokens": ("总结上限", "总结/复盘/周报单次输出上限。", {"min": 100, "max": 4000}),
    "summary.retry_model": ("重试模型", "总结失败时换这个模型再试。", {}),
    "summary.thinking": ("总结思考", "结构化 JSON 不需要思维链，建议 false。", {}),
    "embedding.api": ("向量 API", "embedding 服务地址。", {}),
    "embedding.model": ("向量模型", "默认 embedding-3。", {}),
    "tools.timeout_seconds": ("单工具超时", "单个工具调用最多等多久。", {"min": 5, "max": 600}),
    "tools.total_timeout_seconds": ("工具总预算", "一轮回复里所有工具调用的总上限；会被回复硬线压小。", {"min": 10, "max": 900}),
    "context.history_max_chars": ("历史字符预算", "没有纪要时自动放宽到 4000。", {"min": 500, "max": 20000}),
    "context.history_max_items": ("历史条数", "最多取最近多少条消息。", {"min": 5, "max": 200}),
    "context.history_item_max_chars": ("单条消息上限", "防止一条 8000 字文件反复塞回 prompt。", {"min": 100, "max": 5000}),
    "context.recall_query_items": ("召回拼接条数", "召回 query 额外拼最近几条用户消息。", {"min": 1, "max": 10}),
    "context.summary_idle_minutes": ("空闲总结（分钟）", "距上次总结这么久且有新消息，再触发一次。", {"min": 5, "max": 240}),
    "chat.media_settle_seconds": ("媒体等待（秒）", "图片/文件后等他补一句说明的窗口。", {"min": 0, "max": 10, "step": 0.1}),
    "chat.shadow_followup": ("影子补句", "只记日志、不发消息，用来量「本来可以补的话」。", {}),
    "chat.shadow_sample": ("影子抽样", "0–1，默认 0.3。", {"min": 0, "max": 1, "step": 0.05}),
    "group.enabled": ("启用群聊", "独立链路，无记忆无情绪。", {}),
    "group.whitelist": ("群白名单", "群号列表，一行一个；空 = 任何群都不回。", {"list": "int", "max_items": 20}),
    "group.context_turns": ("群上下文条数", "进程内短期上下文，重启即忘。", {"min": 1, "max": 100}),
    "group.rate_per_minute": ("每群每分钟", "限流。", {"min": 1, "max": 30}),
    "group.cooldown_seconds": ("回复冷却（秒）", "两次回复的最小间隔。", {"min": 0, "max": 60}),
    "group.daily_token_budget": ("每日 token 上限", "每群每天，超了静默停。", {"min": 1000, "max": 1000000}),
    "group.reply_max_tokens": ("群回复上限", "群里说短句，别刷屏。", {"min": 50, "max": 2000}),
    "watch.topics": ("巡检话题", "一行一个；写进 watchlist.json 后可不重启。", {"list": "str", "max_items": 20}),
    "image.max_side": ("图片长边", "1600 能看清手机截图里的小字；1024 更省 token。", {"min": 256, "max": 4096}),
    "image.quality": ("JPEG 质量", "50–100。", {"min": 50, "max": 100}),
    "mood.tau_valence_hours": ("心情衰减 τ", "单次波动回到基线的尺度（小时）。", {"min": 0.5, "max": 240, "step": 0.5}),
    "mood.tau_base_hours": ("基线衰减 τ", "天级基线追随当前心情的尺度。", {"min": 1, "max": 720, "step": 1}),
    "mood.tau_energy_hours": ("精力恢复 τ", "白天闲下来时精力回静息的尺度。", {"min": 0.5, "max": 240, "step": 0.5}),
    "mood.tau_social_hours": ("互动热度 τ", "互动热度的记忆长度。", {"min": 0.5, "max": 240, "step": 0.5}),
    "mood.night_drain": ("夜间掉精力/小时", "0–5 点每小时掉多少。", {"min": 0, "max": 1, "step": 0.01}),
    "mood.sleep_recover_hours": ("晨间恢复 τ", "6–9 点向满电恢复的尺度。", {"min": 0.1, "max": 24, "step": 0.1}),
    "mood.energy_base_cost": ("每条消息基础成本", "精力扣减。", {"min": 0, "max": 0.2, "step": 0.001}),
    "mood.energy_burst_cost": ("连发额外成本", "乘连发系数。", {"min": 0, "max": 0.5, "step": 0.001}),
    "meme.threshold_positive": ("正向阈值", "心情为正时的触发线。", {"min": 0, "max": 1, "step": 0.01}),
    "meme.threshold_neutral": ("中性阈值", "心情接近 0 时的触发线。", {"min": 0, "max": 1, "step": 0.01}),
    "meme.threshold_negative": ("负向阈值", "心情为负时的触发线。", {"min": 0, "max": 1, "step": 0.01}),
    "meme.mood_threshold": ("旧版总阈值", "三档缺省时的回落值，保留兼容。", {"min": 0, "max": 1, "step": 0.01}),
    "memory.dup_window_minutes": ("重复写窗口（分钟）", "同一段文字多久内不写第二条；0 = 关闭。", {"min": 0, "max": 1440}),
    "memory.cleanup_days_important": ("重要记忆保留（天）", "importance ≥ 0.8。", {"min": 1, "max": 3650}),
    "memory.cleanup_days_normal": ("普通记忆保留（天）", "0.5–0.8。", {"min": 1, "max": 3650}),
    "memory.cleanup_days_low": ("低价值保留（天）", "< 0.5。", {"min": 1, "max": 3650}),
    "memory.cleanup_max_active": ("活跃总量兜底", "超过后按重要性×新鲜度淘汰。", {"min": 100, "max": 100000}),
    "memory.recall_min_score": ("召回相似度下限", "0.35 是实测切点。", {"min": 0, "max": 1, "step": 0.01}),
    "memory.recall_time_tau_days": ("时间衰减 τ（天）", "越大越不看重新旧。", {"min": 1, "max": 365}),
    "memory.recall_time_weight": ("时间权重", "时间最多把分数拉低多少。", {"min": 0, "max": 1, "step": 0.05}),
    "memory.recall_importance_weight": ("重要性权重", "重要性最多加多少。", {"min": 0, "max": 1, "step": 0.05}),
    "memory.recall_keep_important": ("重要性保底", "importance ≥ 此值不做时间衰减。", {"min": 0, "max": 1, "step": 0.05}),
    "memory.contradiction_check": ("矛盾检测", "off 不查；shadow 只记；on 真的标取代。", {"options": ["off", "shadow", "on"]}),
    "memory.shadow_retrieval": ("BM25 影子", "只记对比，不改召回结果。", {}),
    "profile.inject": ("画像注入", "off 不注入；shadow 只落盘；on 真拼进 prompt。", {"options": ["off", "shadow", "on"]}),
    "pricing.deepseek-flash.in": ("Flash 输入（未命中）", "元 / 百万 token。", {"min": 0, "max": 1000, "step": 0.1}),
    "pricing.deepseek-flash.out": ("Flash 输出", "元 / 百万 token。", {"min": 0, "max": 1000, "step": 0.1}),
    "pricing.deepseek-flash.cache_in": ("Flash 缓存命中", "约未命中的 1/4。", {"min": 0, "max": 1000, "step": 0.1}),
    "pricing.deepseek-chat.in": ("Chat 输入（未命中）", "元 / 百万 token。", {"min": 0, "max": 1000, "step": 0.1}),
    "pricing.deepseek-chat.out": ("Chat 输出", "元 / 百万 token。", {"min": 0, "max": 1000, "step": 0.1}),
    "pricing.deepseek-chat.cache_in": ("Chat 缓存命中", "约未命中的 1/4。", {"min": 0, "max": 1000, "step": 0.1}),
    "pricing.cache_in": ("旧版缓存价", "兼容字段；按模型分档后一般不用动。", {"min": 0, "max": 1000, "step": 0.1}),
}


def _type_of(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return "str"


def _walk(prefix, value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(prefix + (str(k),), v)
    else:
        yield prefix, value


_SCHEMA = None


def build_schema() -> list[dict]:
    global _SCHEMA
    if _SCHEMA is not None:
        return _SCHEMA
    groups = []
    for gname, gval in DEFAULTS.items():
        fields = []
        for path, default in _walk((gname,), gval):
            fid = ".".join(path)
            label, desc, extra = F.get(fid, (path[-1], "", {}))
            field = {
                "id": fid, "path": list(path), "key": path[-1],
                "label": label, "desc": desc, "type": _type_of(default),
                "default": default,
            }
            field.update(extra or {})
            fields.append(field)
        glabel, gdesc = GROUP_META.get(gname, (gname, ""))
        groups.append({"id": gname, "label": glabel, "desc": gdesc, "fields": fields})
    _SCHEMA = groups
    return groups


def fields_by_id() -> dict[str, dict]:
    return {f["id"]: f for g in build_schema() for f in g["fields"]}


# ------------------------------------------------------------- yaml io ------
def _yaml():
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_doc(path: Path):
    if not path.exists():
        return CommentedMap()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return CommentedMap()
    doc = _yaml().load(text)
    if doc is None:
        return CommentedMap()
    if not isinstance(doc, dict):
        raise ValueError("config.yaml 顶层不是 mapping")
    return doc


def dump_doc(doc) -> str:
    buf = StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def deep_get(doc, path: list[str]):
    cur = doc
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def deep_set(doc, path: list[str], value) -> None:
    cur = doc
    for k in path[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = CommentedMap()
        cur = cur[k]
    cur[path[-1]] = value


def collect_values(doc) -> dict:
    out = {}
    for g in build_schema():
        for f in g["fields"]:
            exists, val = deep_get(doc, f["path"])
            out[f["id"]] = val if exists else f["default"]
    return out


def unknown_fields(doc) -> list[dict]:
    known = {tuple(f["path"]) for g in build_schema() for f in g["fields"]}
    out = []

    def walk(prefix, value):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(prefix + (str(k),), v)
        elif prefix not in known:
            out.append({"id": ".".join(prefix), "value": value})

    walk((), doc)
    return out


def same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


# ------------------------------------------------------------- validate -----
def _err(fid, msg):
    return f"{fid}: {msg}"


def validate_payload(values: dict) -> tuple[dict, list[str]]:
    """把 UI 传来的 JSON 值校验/归一化。返回 (coerced, errors)。"""
    by_id = fields_by_id()
    out, errors = {}, []
    if not isinstance(values, dict):
        return {}, ["values 必须是对象"]
    for fid, raw in values.items():
        f = by_id.get(fid)
        if not f:
            errors.append(_err(fid, "未知字段（不会写入）"))
            continue
        t = f["type"]
        v = raw
        if t == "bool":
            if not isinstance(v, bool):
                errors.append(_err(fid, "需要 true / false"))
                continue
        elif t == "int":
            if isinstance(v, bool) or not isinstance(v, int):
                errors.append(_err(fid, "需要整数"))
                continue
        elif t == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append(_err(fid, "需要数字"))
                continue
            v = float(v)
        elif t == "str":
            if not isinstance(v, str):
                errors.append(_err(fid, "需要字符串"))
                continue
            v = v.strip()
            if f.get("options"):
                if v not in f["options"]:
                    errors.append(_err(fid, "必须是 " + " / ".join(f["options"])))
                    continue
            elif fid == "admin.user_id":
                if v and not re.fullmatch(r"\d{4,20}", v):
                    errors.append(_err(fid, "QQ 号只能是数字"))
                    continue
            elif fid in ("llm.api", "embedding.api"):
                if not re.match(r"^https?://", v):
                    errors.append(_err(fid, "需要 http(s) 地址"))
                    continue
            elif fid not in ("admin.user_id",) and not v:
                errors.append(_err(fid, "不能为空"))
                continue
        elif t == "list":
            if not isinstance(v, list):
                errors.append(_err(fid, "需要列表"))
                continue
            kind = f.get("list", "str")
            if kind == "int":
                try:
                    v = [int(x) for x in v]
                except Exception:
                    errors.append(_err(fid, "列表项必须是整数"))
                    continue
            else:
                v = [str(x).strip() for x in v if str(x).strip()]
            if len(v) > int(f.get("max_items", 50)):
                errors.append(_err(fid, f"最多 {f['max_items']} 项"))
                continue
        if "min" in f and isinstance(v, (int, float)) and not isinstance(v, bool) and v < f["min"]:
            errors.append(_err(fid, f"不能小于 {f['min']}"))
            continue
        if "max" in f and isinstance(v, (int, float)) and not isinstance(v, bool) and v > f["max"]:
            errors.append(_err(fid, f"不能大于 {f['max']}"))
            continue
        out[fid] = v
    return out, errors


def apply_values(doc, values: dict) -> None:
    by_id = fields_by_id()
    for fid, v in values.items():
        f = by_id.get(fid)
        if f:
            deep_set(doc, f["path"], v)


# ------------------------------------------------------------- save ---------
def make_diff(old: str, new: str, fromfile="config.yaml（当前）",
              tofile="config.yaml（保存后）") -> str:
    return "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        (new or "").splitlines(keepends=True),
        fromfile=fromfile, tofile=tofile, lineterm="\n"))


def backup_path(backup_dir: Path, prefix: str, suffix: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = backup_dir / f"{prefix}-{stamp}{suffix}"
    n = 1
    while p.exists():
        p = backup_dir / f"{prefix}-{stamp}-{n}{suffix}"
        n += 1
    return p


def write_file(path: Path, backup_dir: Path, old_text: str, new_text: str,
               prefix: str, suffix: str) -> str:
    """备份 + 原子写。返回备份文件名（没有旧文件时为空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if path.exists():
        bp = backup_path(backup_dir, prefix, suffix)
        bp.write_bytes(path.read_bytes())
        backup = str(bp)
        try:
            olds = sorted(backup_dir.glob(f"{prefix}-*{suffix}"), key=lambda x: x.name)
            for p in olds[:-30]:
                p.unlink(missing_ok=True)
        except Exception:
            pass
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{prefix}-", suffix=suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return backup


def write_config(config_path: Path, backup_dir: Path, old_text: str, new_text: str) -> str:
    return write_file(config_path, backup_dir, old_text, new_text, "config", ".yaml")


# ------------------------------------------------------- persona / watch -----
PERSONAS = {
    "owner": ("本人", "persona.json"),
    "stranger": ("外人版", "persona-stranger.json"),
}
MAX_WATCH_TOPICS = 8


def data_path(app, name: str) -> Path:
    return app.config_path.parent / "data" / name


def read_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(obj, indent: int = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"


def validate_persona(data: dict) -> tuple[dict, list[str]]:
    errors = []
    prompt = data.get("system_prompt")
    dialogs = data.get("begin_dialogs")
    if not isinstance(prompt, str):
        errors.append("system_prompt: 需要字符串")
        prompt = ""
    elif not prompt.strip():
        errors.append("system_prompt: 不能为空")
    elif len(prompt) > 20000:
        errors.append("system_prompt: 最多 20000 字")
    if not isinstance(dialogs, list):
        errors.append("begin_dialogs: 需要列表（一行一条，user/assistant 交替）")
        dialogs = []
    else:
        if len(dialogs) % 2:
            errors.append("begin_dialogs: 条数必须是偶数（user, assistant 成对）")
        if len(dialogs) > 200:
            errors.append("begin_dialogs: 最多 200 条")
        clean = []
        for i, x in enumerate(dialogs):
            if not isinstance(x, str):
                errors.append(f"begin_dialogs[{i}]: 需要字符串")
                continue
            if len(x) > 2000:
                errors.append(f"begin_dialogs[{i}]: 单条最多 2000 字")
                continue
            clean.append(x)
        dialogs = clean
    return {"system_prompt": prompt, "begin_dialogs": dialogs}, errors


def apply_persona(old: dict | None, values: dict) -> dict:
    """只改 system_prompt / begin_dialogs，其他字段原样保留。"""
    doc = dict(old or {})
    doc["system_prompt"] = values["system_prompt"]
    doc["begin_dialogs"] = values["begin_dialogs"]
    return doc


def validate_watch(data: dict) -> tuple[list[str], list[str]]:
    topics = data.get("topics")
    errors = []
    if not isinstance(topics, list):
        return [], ["topics: 需要列表"]
    out, seen = [], set()
    for i, x in enumerate(topics):
        t = str(x or "").strip()
        if not t:
            continue
        if len(t) > 80:
            errors.append(f"topics[{i}]: 最多 80 字")
            continue
        if t in seen:
            errors.append(f"topics[{i}]: 重复「{t}」")
            continue
        seen.add(t)
        out.append(t)
    if len(out) > MAX_WATCH_TOPICS:
        errors.append(f"topics: 最多 {MAX_WATCH_TOPICS} 个（watch.MAX_TOPICS）")
    return out, errors


def merge_watch(old_items, topics: list[str]) -> list[dict]:
    """保留同名话题的 last/ts 基线；新话题从空开始，删掉的不再写回。"""
    old = {}
    for it in old_items if isinstance(old_items, list) else []:
        if isinstance(it, str):
            old[it.strip()] = {"topic": it.strip(), "last": "", "ts": 0}
        elif isinstance(it, dict) and str(it.get("topic") or "").strip():
            t = str(it["topic"]).strip()
            old[t] = {"topic": t, "last": str(it.get("last") or ""),
                      "ts": float(it.get("ts") or 0)}
    out = []
    for t in topics:
        out.append(old.get(t) or {"topic": t, "last": "", "ts": 0})
    return out


# ------------------------------------------------------------- service ------
def service_status(config_path: Path) -> dict | None:
    try:
        p = subprocess.run(
            ["systemctl", "show", "nonebot", "-p", "ActiveState",
             "-p", "ActiveEnterTimestamp", "-p", "ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=2)
        if p.returncode != 0:
            return None
        props = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
        state = props.get("ActiveState", "")
        since = props.get("ActiveEnterTimestamp", "")
        stale = None
        try:
            mono = float(props.get("ActiveEnterTimestampMonotonic") or 0) / 1_000_000
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            start = time.time() - (uptime - mono)
            if config_path.exists() and state == "active":
                stale = config_path.stat().st_mtime > start + 1
        except Exception:
            stale = None
        return {"state": state, "since": since, "stale": stale}
    except Exception:
        return None


# ------------------------------------------------------------- app ----------
@dataclass
class App:
    config_path: Path
    backup_dir: Path
    token: str
    port: int

    @property
    def origins(self):
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    @property
    def hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore

    server_version = "companion-config-ui"

    def log_message(self, fmt, *args):
        # 默认日志会把 ?token= 打进 journal，这里只记路径
        log.debug("%s %s", self.command, urlparse(self.path).path)

    # ---- helpers ----
    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.app.hosts

    def _origin_ok(self) -> bool:
        o = self.headers.get("Origin")
        return not o or o in self.app.origins

    def _auth(self) -> bool:
        q = parse_qs(urlparse(self.path).query)
        got = (q.get("token") or [None])[0]
        if not got:
            got = self.headers.get("X-Token")
        if not got:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            m = c.get(COOKIE)
            got = m.value if m else None
        return bool(got) and hmac.compare_digest(str(got), self.app.token)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name: str):
        p = STATIC / name
        if not p.is_file():
            return self._text("missing static file", 500)
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8"}.get(p.suffix.lstrip("."), "application/octet-stream")
        return self._text(p.read_text(encoding="utf-8"), 200, ctype)

    def _payload(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            raise ValueError("请求体为空或太大")
        data = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象")
        return data

    # ---- routes ----
    def do_GET(self):
        if not self._host_ok():
            return self._json({"ok": False, "error": "Host 不允许"}, 421)
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._json({"ok": True})
        if u.path == "/":
            q = parse_qs(u.query)
            if (q.get("token") or [""])[0] and self._auth():
                self.send_response(302)
                self.send_header("Set-Cookie", f"{COOKIE}={self.app.token}; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Location", "/")
                self.end_headers()
                return
            if not self._auth():
                return self._text("需要 token：在地址后加 ?token=<token>（见 config-ui 启动日志）", 401)
            return self._file("index.html")
        if u.path in ("/app.js", "/style.css"):
            if not self._auth():
                return self._text("unauthorized", 401)
            return self._file(u.path.lstrip("/"))
        if u.path == "/api/config":
            if not self._auth():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            try:
                doc = load_doc(self.app.config_path)
                groups = []
                for g in build_schema():
                    fields = []
                    for f in g["fields"]:
                        exists, val = deep_get(doc, f["path"])
                        value = val if exists else f["default"]
                        item = dict(f)
                        item.update({
                            "value": value,
                            "source": "file" if exists else "default",
                            "changed": bool(exists and not same(val, f["default"])),
                        })
                        fields.append(item)
                    groups.append({**g, "fields": fields})
                mtime = self.app.config_path.stat().st_mtime if self.app.config_path.exists() else 0
                return self._json({
                    "ok": True,
                    "path": str(self.app.config_path),
                    "exists": self.app.config_path.exists(),
                    "mtime": mtime,
                    "mtime_text": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "—",
                    "groups": groups,
                    "unknown": unknown_fields(doc),
                    "service": service_status(self.app.config_path),
                    "restart_cmd": "sudo systemctl restart nonebot",
                })
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if u.path == "/api/assets":
            if not self._auth():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            try:
                return self._json(self._assets())
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if u.path == "/api/backups":
            if not self._auth():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            rows = []
            try:
                for p in sorted(self.app.backup_dir.glob("config-*.yaml"), key=lambda x: x.name, reverse=True)[:30]:
                    rows.append({"name": p.name, "size": p.stat().st_size})
            except Exception:
                pass
            return self._json({"ok": True, "backups": rows})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"ok": False, "error": "Host 不允许"}, 421)
        if not self._origin_ok():
            return self._json({"ok": False, "error": "Origin 不允许"}, 403)
        if not self._auth():
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        try:
            data = self._payload()
            if path == "/api/config":
                return self._save_config(data)
            if path == "/api/persona":
                return self._save_persona(data)
            if path == "/api/watchlist":
                return self._save_watch(data)
            return self._json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    # ---- assets ----
    def _assets(self):
        personas = []
        for which, (label, fname) in PERSONAS.items():
            p = data_path(self.app, fname)
            mtime = p.stat().st_mtime if p.exists() else 0
            item = {
                "which": which, "label": label, "path": str(p),
                "exists": p.exists(), "mtime": mtime,
                "mtime_text": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "—",
                "system_prompt": "", "begin_dialogs": [], "meta": {}, "error": "",
            }
            if p.exists():
                try:
                    d = read_json(p)
                    if not isinstance(d, dict):
                        raise ValueError("顶层不是对象")
                    item["system_prompt"] = str(d.get("system_prompt") or "")
                    dialogs = d.get("begin_dialogs")
                    item["begin_dialogs"] = [str(x) for x in dialogs] if isinstance(dialogs, list) else []
                    item["meta"] = {k: v for k, v in d.items()
                                    if k not in ("system_prompt", "begin_dialogs")
                                    and isinstance(v, (str, int, float, bool))}
                except Exception as e:
                    item["error"] = f"{type(e).__name__}: {e}"
            personas.append(item)

        wp = data_path(self.app, "watchlist.json")
        wmtime = wp.stat().st_mtime if wp.exists() else 0
        items = []
        error = ""
        if wp.exists():
            try:
                raw = read_json(wp)
                for it in raw if isinstance(raw, list) else []:
                    if isinstance(it, str):
                        items.append({"topic": it.strip(), "last": "", "ts": 0})
                    elif isinstance(it, dict) and str(it.get("topic") or "").strip():
                        items.append({"topic": str(it["topic"]).strip(),
                                      "last": str(it.get("last") or ""),
                                      "ts": float(it.get("ts") or 0)})
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
        return {
            "ok": True,
            "personas": personas,
            "watchlist": {
                "path": str(wp), "exists": wp.exists(), "mtime": wmtime,
                "mtime_text": datetime.fromtimestamp(wmtime).strftime("%Y-%m-%d %H:%M:%S") if wmtime else "—",
                "topics": items, "max": MAX_WATCH_TOPICS, "error": error,
            },
        }

    # ---- save handlers ----
    def _save_config(self, data: dict):
        validated, errors = validate_payload(data.get("values") or {})
        if errors:
            return self._json({"ok": False, "errors": errors}, 400)
        old_text = self.app.config_path.read_text(encoding="utf-8") if self.app.config_path.exists() else ""
        doc = load_doc(self.app.config_path)
        apply_values(doc, validated)
        new_text = dump_doc(doc)
        diff = make_diff(old_text, new_text)
        if data.get("dry_run"):
            return self._json({"ok": True, "diff": diff, "changed": old_text != new_text})
        if old_text == new_text:
            return self._json({"ok": True, "unchanged": True, "diff": ""})
        backup = write_config(self.app.config_path, self.app.backup_dir, old_text, new_text)
        log.info("保存配置：%s（备份 %s）", self.app.config_path, backup or "无")
        return self._json({
            "ok": True, "backup": backup, "diff": diff,
            "restart_cmd": "sudo systemctl restart nonebot",
            "service": service_status(self.app.config_path),
        })

    def _save_persona(self, data: dict):
        which = str(data.get("which") or "")
        if which not in PERSONAS:
            return self._json({"ok": False, "error": "which 必须是 owner / stranger"}, 400)
        values, errors = validate_persona(data)
        if errors:
            return self._json({"ok": False, "errors": errors}, 400)
        label, fname = PERSONAS[which]
        path = data_path(self.app, fname)
        old_text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            old = read_json(path) if path.exists() else {}
        except Exception as e:
            return self._json({"ok": False, "error": f"{fname} 不是合法 JSON：{e}"}, 400)
        if not isinstance(old, dict):
            return self._json({"ok": False, "error": f"{fname} 顶层不是对象"}, 400)
        new_doc = apply_persona(old, values)
        new_text = dump_json(new_doc, 2)
        diff = make_diff(old_text, new_text,
                         fromfile=f"{label}人格（当前）", tofile=f"{label}人格（保存后）")
        # 只比较内容：文件原本的缩进/换行不同，不代表用户改了人格。
        changed = (not path.exists()) or old != new_doc
        if data.get("dry_run"):
            return self._json({"ok": True, "diff": diff, "changed": changed})
        if not changed:
            return self._json({"ok": True, "unchanged": True, "diff": ""})
        backup = write_file(path, self.app.backup_dir, old_text, new_text,
                            f"persona-{which}", ".json")
        log.info("保存人格：%s（备份 %s）", path, backup or "无")
        return self._json({
            "ok": True, "backup": backup, "diff": diff,
            "reload_cmd": "在 QQ 发 /reload_persona（或重启 nonebot）",
        })

    def _save_watch(self, data: dict):
        topics, errors = validate_watch(data)
        if errors:
            return self._json({"ok": False, "errors": errors}, 400)
        path = data_path(self.app, "watchlist.json")
        old_text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            old = read_json(path) if path.exists() else []
        except Exception as e:
            return self._json({"ok": False, "error": f"watchlist.json 不是合法 JSON：{e}"}, 400)
        new_items = merge_watch(old, topics)
        old_topics = []
        for x in old if isinstance(old, list) else []:
            if isinstance(x, str):
                old_topics.append(x.strip())
            elif isinstance(x, dict) and str(x.get("topic") or "").strip():
                old_topics.append(str(x["topic"]).strip())
        old_items = merge_watch(old, old_topics)
        new_text = dump_json(new_items, 1)
        diff = make_diff(old_text, new_text,
                         fromfile="watchlist.json（当前）", tofile="watchlist.json（保存后）")
        # 只比内容：旧的缩进/浮点写法不同不算改动。删掉话题或改 last 才算。
        changed = (not path.exists()) or old_items != new_items
        if data.get("dry_run"):
            return self._json({"ok": True, "diff": diff, "changed": changed})
        if not changed:
            return self._json({"ok": True, "unchanged": True, "diff": ""})
        backup = write_file(path, self.app.backup_dir, old_text, new_text,
                            "watchlist", ".json")
        log.info("保存巡检话题：%s（备份 %s）", path, backup or "无")
        return self._json({
            "ok": True, "backup": backup, "diff": diff, "hot_reload": True,
            "note": "免重启，下一次巡检读取这个文件",
        })


def load_token(path: Path) -> str:
    try:
        tok = path.read_text(encoding="utf-8").strip()
    except Exception:
        tok = ""
    if not tok:
        tok = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tok + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    return tok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="companion-bot 本地配置面板")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--port", type=int, default=8110)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--backup-dir", type=Path, default=None)
    ap.add_argument("--token-file", type=Path, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost"):
        print("只允许绑定 IPv4 回环地址（127.0.0.1 / localhost）", file=sys.stderr)
        return 2
    config_path = args.config.expanduser().resolve()
    backup_dir = args.backup_dir or (config_path.parent / "config-backups")
    token_file = args.token_file or (config_path.parent / "data" / "config-ui.token")
    token = load_token(token_file)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    app = App(config_path=config_path, backup_dir=backup_dir, token=token, port=args.port)
    Handler.app = app

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True

    httpd = Server((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/?token={token}"
    print(f"配置面板已启动：{url}", flush=True)
    print(f"配置文件：{config_path}", flush=True)
    print(f"备份目录：{backup_dir}", flush=True)
    print(f"访问：ssh -L {args.port}:127.0.0.1:{args.port} root@<server>", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
