"""网页学习：抓正文、清理、LLM 提炼要点。"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from datetime import datetime
from pathlib import Path

from app_core import paths as _paths
from urllib.parse import urlparse

import httpx

from app_core.files import sanitize

KEYS = _paths.KEYS
SEARCH_API = "https://api.deepseek.com/v1/tools/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MAX_CHARS = 4000     # 返回给用户的正文上限
MAX_BYTES = 5_000_000  # 单个页面下载上限，防止把 3.7G 内存的机器打满
SAFE_PORTS = {80, 443, 8080, 8443}
MIN_DIGEST = 800     # 正文超过这个长度才值得让模型提炼
# 提炼时给模型的原文上限，比 MAX_CHARS 长，让提炼能多看一点上下文
DIGEST_CHARS = 6000

SYSTEM = (
    "你在帮一个长期陪伴型 AI 助手做「网页学习笔记」。"
    "读一篇网页正文，提炼真正值得长期记住的东西：有用的方法、结论、数据、可复用经验。"
    "跳过广告、导航、客套和时效性太强的内容。"
    "用中文 Markdown 输出，最多 6 条要点，每条一句话，总长 400 字以内。"
    "如果整篇没有值得长期保留的内容，只输出两个字：无。"
)


def _deepseek_key() -> str:
    try:
        return json.loads(KEYS.read_text(encoding="utf-8")).get("deepseek", "")
    except Exception:
        return ""


_client = None
_client_cls = None


def _http() -> httpx.AsyncClient:
    """共享的抓取客户端：省掉每次 web_open 的 TLS 握手（约 100–200ms）。

    「类被替换就重建」是给测试留的口子：测试里 patch 了 httpx.AsyncClient
    之后，缓存必须失效，否则 mock 不生效。
    """
    global _client, _client_cls
    cls = httpx.AsyncClient
    closed = getattr(_client, "is_closed", False) if _client is not None else True
    if _client is None or closed or cls is not _client_cls:
        _client = cls(timeout=httpx.Timeout(25.0, connect=10.0),
                      follow_redirects=True, headers={"User-Agent": UA},
                      limits=httpx.Limits(max_connections=4,
                                          max_keepalive_connections=2))
        _client_cls = cls
    return _client


async def search(query: str, count: int = 5) -> str:
    """联网搜索。DeepSeek 官方 Search API，5 元 / 1000 次。"""
    q = (query or "").strip()[:128]
    if not q:
        return "搜索词为空"
    count = max(1, min(int(count or 5), 10))
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                SEARCH_API,
                headers={"Authorization": "Bearer " + _deepseek_key(),
                         "Content-Type": "application/json"},
                json={"query": q, "count": count, "need_content": False},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        return f"搜索失败（{type(e).__name__}）"

    results = data.get("results") or []
    if not results:
        return f"没搜到「{q}」的结果"
    lines = []
    for i, it in enumerate(results[:count], 1):
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        summary = (it.get("summary") or it.get("snippet") or "").strip()
        summary = re.sub(r"\s+", " ", summary)[:240]
        date = (it.get("date") or "")[:10]
        tail = f"（{date}）" if date else ""
        lines.append(f"{i}. {title}{tail}\n   {summary}\n   {url}")
    return "搜索结果：\n" + "\n\n".join(lines)


def _safe(url: str) -> bool:
    """SSRF 防护：只允许公网 http(s)。

    光挡 localhost/127.0.0.1 不够——云主机上的 169.254.169.254 是元数据接口，
    10./192.168./172.16. 是内网。这里把域名解析出的**所有**地址都检查一遍，
    顺带挡掉 DNS rebinding（解析到内网就拒绝）。
    """
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        port = u.port or (443 if u.scheme == "https" else 80)
        if port not in SAFE_PORTS:
            return False
        infos = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def extract_text(html: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:100]
    body = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?is)<br\s*/?>", "\n", body)
    body = re.sub(r"(?is)</p>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"&nbsp;", " ", body)
    body = re.sub(r"&amp;", "&", body)
    body = re.sub(r"&lt;", "<", body)
    body = re.sub(r"&gt;", ">", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    # 网页同样可能藏隐形指令，进上下文前先剥掉
    return sanitize(title), sanitize(body.strip())


async def fetch(url: str) -> tuple[str, str, int]:
    c = _http()
    async with c.stream("GET", url) as r:
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_BYTES:
                return "", str(r.url), 413
        enc = r.encoding or "utf-8"
        return bytes(buf).decode(enc, "replace"), str(r.url), r.status_code


async def web_open(core, url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    if not _safe(url):
        return f"这个地址看起来不合法：{url[:80]}"
    try:
        html, final, status = await fetch(url)
    except Exception as e:  # noqa: BLE001
        return f"打不开（{type(e).__name__}）"
    title, text = extract_text(html)
    if len(text) < 200:
        return f"页面几乎没抓到正文（{len(text)} 字，HTTP {status}），可能需要 JS 渲染"
    body = text[:MAX_CHARS]
    digest = ""
    if len(text) >= MIN_DIGEST:
        try:
            # 提炼是总结任务，不需要思考模式（思考会先吃掉输出预算）
            msg = await core.complete_raw(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": f"标题：{title}\n\n正文：\n{text[:DIGEST_CHARS]}"}],
                max_tokens=600, thinking=False, purpose="web")
            digest = (msg.get("content") or "").strip()
        except Exception:
            digest = ""
    if digest and digest.strip() not in ("无", "無"):
        from app_core import learn
        name = f"网页-{title or '无标题'}"
        # 存原始地址和抓取时间：不存的话这篇笔记以后无法回溯，
        # 模型只能把它当成「自己知道的事」，而它其实只是一段二手转述。
        learn.note_write(name, digest, tag="网页学习",
                         source=f"网页 {final}（{datetime.now():%Y-%m-%d %H:%M} 抓取）")
        return f"[标题] {title}\n[正文]\n{body}\n\n[已提炼要点，存入学习笔记]"
    return f"[标题] {title}\n[正文]\n{body}"
