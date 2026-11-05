"""沙箱出网代理：只放行白名单域名，其余一律拒绝并记日志。

为什么自己写而不上 squid/tinyproxy：
- 只需要「按域名放行 CONNECT」这一件事，80 行够了，可审计
- 服务器上已经有 python:3.12-slim 镜像，不用再拉新镜像（国内拉镜像很慢）

工作方式：
    sandbox --(agentd-net/内网)--> 本代理 --(bridge)--> 互联网

HTTPS 走 CONNECT：先看 host，允许才建隧道，**不做中间人**（不碰证书）。
HTTP 走绝对 URL / Host 头。
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
from urllib.parse import urlsplit

PORT = int(os.environ.get("PROXY_PORT", "8888"))


def _nodelay(writer: asyncio.StreamWriter) -> None:
    """关掉 Nagle。

    不加这个，代理转发小包会撞上 delayed-ACK：每 40ms 才走一批，
    实测 82KB 要 6 秒——pip 直接把它当握手超时。
    """
    try:
        s = writer.get_extra_info("socket")
        if s is not None:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:  # noqa: BLE001
        pass

# 白名单：包源 + 代码托管 + 官方文档/API。
# 搜索不靠这里——agentd 的 search 工具在宿主侧调 DeepSeek Search API，
# 结果质量不受代理影响；沙箱出网只解决「装包、clone、抓文档」。
ALLOW = [
    # pip / npm
    r"(^|\.)pypi\.org$", r"(^|\.)files\.pythonhosted\.org$",
    r"(^|\.)pypi\.tuna\.tsinghua\.edu\.cn$",
    r"(^|\.)registry\.npmmirror\.com$", r"(^|\.)npmjs\.org$",
    r"(^|\.)nodejs\.org$",
    # apt
    r"(^|\.)deb\.debian\.org$", r"(^|\.)security\.debian\.org$",
    r"(^|\.)mirrors\.tuna\.tsinghua\.edu\.cn$",
    r"(^|\.)archive\.ubuntu\.com$", r"(^|\.)mirrors\.aliyun\.com$",
    # 代码
    r"(^|\.)github\.com$", r"(^|\.)codeload\.github\.com$",
    r"(^|\.)raw\.githubusercontent\.com$", r"(^|\.)api\.github\.com$",
    r"(^|\.)gitee\.com$", r"(^|\.)objects\.githubusercontent\.com$",
    # 文档 / API
    r"(^|\.)api\.deepseek\.com$",
    r"(^|\.)docs\.python\.org$", r"(^|\.)developer\.mozilla\.org$",
    r"(^|\.)pypi\.org$", r"(^|\.)readthedocs\.io$",
    r"(^|\.)docs\.docker\.com$", r"(^|\.)man7\.org$",
]
ALLOW_RE = [re.compile(p, re.I) for p in ALLOW]


def allowed(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    return any(r.search(host) for r in ALLOW_RE)


def log(msg: str) -> None:
    print(msg, flush=True)


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readline(), timeout=15)
        if not head:
            return
        line = head.decode("latin-1").strip()
        parts = line.split()
        if len(parts) < 2:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        method, target = parts[0].upper(), parts[1]

        if method == "CONNECT":
            host = target.rsplit(":", 1)[0]
            if not allowed(host):
                log(f"[proxy] DENY CONNECT {host}")
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            # 把剩下的头吃掉（不转发，避免泄漏）
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=10)
                if h in (b"\r\n", b"\n", b""):
                    break
            try:
                up_r, up_w = await asyncio.wait_for(
                    asyncio.open_connection(host, 443), timeout=15)
            except Exception as e:  # noqa: BLE001
                log(f"[proxy] FAIL {host} {type(e).__name__}")
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            _nodelay(writer)
            _nodelay(up_w)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            log(f"[proxy] ALLOW CONNECT {host}")
            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
            return

        # 明文 HTTP：从 Host 头判断
        host = ""
        while True:
            h = await asyncio.wait_for(reader.readline(), timeout=10)
            if h in (b"\r\n", b"\n", b""):
                break
            if h.lower().startswith(b"host:"):
                host = h.split(b":", 1)[1].strip().decode("latin-1")
        if target.startswith("http://") or target.startswith("https://"):
            host = urlsplit(target).hostname or host
        if not allowed(host):
            log(f"[proxy] DENY {method} {host}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(host, 80), timeout=15)
        except Exception as e:  # noqa: BLE001
            log(f"[proxy] FAIL {host} {type(e).__name__}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        _nodelay(writer)
        _nodelay(up_w)
        log(f"[proxy] ALLOW {method} {host}")
        up_w.write(f"{method} {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await up_w.drain()
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log(f"[proxy] listen :{PORT}, allow {len(ALLOW)} patterns")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
