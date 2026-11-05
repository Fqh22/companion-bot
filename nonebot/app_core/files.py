"""文件解析：把常见格式读成文本。"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from pathlib import Path

SAVE_DIR = Path("/opt/nonebot/data/files")
# 「刚收到的文件」登记处：/task 和 agent_task 都从这里取。
PENDING_JSON = Path("/opt/nonebot/data/last-file.json")
AGENT_INBOX = Path("/opt/agentd/inbox")
PENDING_TTL = 1800
MAX_BYTES = 30 * 1024 * 1024   # 单文件下载上限，防止把内存和磁盘打满
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
    ".ini", ".conf", ".toml", ".xml", ".html", ".htm", ".sql",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb",
    ".php", ".sh", ".bat", ".ps1", ".css", ".vue", ".jsx", ".tsx",
}
MAX_TEXT = 12000
# 解析产出上限。注意：聊天注入时会再截到 8000 字（见 plugins/chat），
# 这里留 12000 是给命令类场景（/diary_write 等）用全文。

# ---------- 图片 ----------
IMAGE_DIR = SAVE_DIR / "images"
# 长边上限。1024 对手机截图太小：1080×2400 缩到 1024 后正文只剩约 5px，
# 实测把 `SYNC_FAILED_0x8F21` 读成 `SYNC_FAILED_0042`、`/srv/app/` 读成 `/usr/app/`；
# 1600 全部读对，代价是 prompt tokens 540 → 1020、首字 +1.5s。
#
# 试过按「是不是截图」分档（5bit 唯一色数、8×8 纯色块比例），都分不开：
# 线上 16 张真实截图色数 1.5k–27k、平铺度 0.08–0.81，和照片区间重叠。
# 所以不做猜测，统一按截图给足分辨率；嫌慢就在 config 的 image.max_side 调回 1024。
MAX_IMAGE_SIDE = 1600
IMAGE_QUALITY = 90
MAX_IMAGES = 3            # 一条消息最多处理几张
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


# 不可见字符：零宽空格/连接符、单词连接符、BOM，以及 Unicode tag block（U+E0000–E007F）。
# 后者能把整段指令藏成"看不见的字符"——外部文档进上下文之前必须剥掉。
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_TAG_BLOCK = re.compile("[\U000E0000-\U000E007F]")


def sanitize(text: str) -> str:
    """剥掉文档里的隐形字符。只删不可见控制字符，正文一个字节都不动。"""
    if not text:
        return ""
    return _TAG_BLOCK.sub("", (text or "").translate(_INVISIBLE))


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", name or "file")
    return name[-80:]


async def save_from_segment(bot, seg) -> tuple[Path | None, str]:
    """把 OneBot 的 file 段落到本地，返回 (路径, 文件名)。"""
    data = getattr(seg, "data", None) or {}
    name = str(data.get("file") or data.get("name") or "file")
    file_id = str(data.get("file_id") or data.get("file") or name)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    target = SAVE_DIR / _safe_name(name)

    raw = ""
    url = ""
    try:
        info = await bot.call_api("get_file", file_id=file_id)
        if isinstance(info, dict):
            raw = str(info.get("file") or "")
            url = str(info.get("url") or "")
    except Exception:
        pass

    if raw.startswith("base64://"):
        try:
            target.write_bytes(base64.b64decode(raw[len("base64://"):]))
            return target, name
        except Exception:
            pass
    if raw and Path(raw).exists():
        try:
            target.write_bytes(Path(raw).read_bytes())
            return target, name
        except Exception:
            pass

    url = url or str(data.get("url") or "")
    if url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code == 200:
                        buf = bytearray()
                        async for chunk in r.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > MAX_BYTES:
                                return None, name
                        target.write_bytes(bytes(buf))
                        return target, name
        except Exception:
            pass
    return None, name


# ---------- 图片处理 ----------
def _img_cfg() -> dict:
    """config.yaml 的 image 段。

    注意：`config.load()` 是进程内缓存，改完配置文件要**重启**才生效
    （docs/03 也是这么写的）。这里每次调用都读，是为了让「重启后」的改动覆盖到，
    不是热加载。
    """
    try:
        from app_core import config

        return config.load().get("image") or {}
    except Exception:
        return {}


def _shrink(raw: bytes) -> bytes | None:
    """压到长边上限的 JPEG。默认 1600，可在 config 的 image.max_side 调。

    为什么给足分辨率：手机截图是 1080×2400，缩到长边 1024 后 13px 正文只剩约 5px，
    实测同一张报错截图，1024 把 `SYNC_FAILED_0x8F21` 读成 `SYNC_FAILED_0042`、
    `/srv/app/` 读成 `/usr/app/`；1600 全部读对。代价是 prompt tokens 540 → 1020。
    """
    try:
        from io import BytesIO

        from PIL import Image, ImageOps
    except Exception:
        return None
    try:
        img = Image.open(BytesIO(raw))
        img.load()
        img = ImageOps.exif_transpose(img)      # 手机照片常带旋转信息，不纠正会歪着看
    except Exception:
        return None
    cfg = _img_cfg()
    try:
        side = int(cfg.get("max_side", MAX_IMAGE_SIDE))
        quality = int(cfg.get("quality", IMAGE_QUALITY))
    except Exception:
        side, quality = MAX_IMAGE_SIDE, IMAGE_QUALITY
    try:
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((side, side), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except Exception:
        return None


async def save_image(bot, seg) -> Path | None:
    """把 OneBot 的 image 段下载并压缩，返回本地路径。"""
    import httpx

    data = getattr(seg, "data", None) or {}
    raw = b""
    url = str(data.get("url") or "")
    if url:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code == 200:
                        buf = bytearray()
                        async for chunk in r.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > MAX_BYTES:
                                return None
                        raw = bytes(buf)
        except Exception:
            raw = b""
    if not raw:
        file_id = str(data.get("file") or "")
        if file_id:
            try:
                info = await bot.call_api("get_image", file=file_id)
                if isinstance(info, dict):
                    u = str(info.get("url") or "")
                    if u:
                        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                            r = await c.get(u)
                            if r.status_code == 200:
                                raw = r.content
            except Exception:
                pass
    if not raw:
        return None
    small = _shrink(raw)
    if not small:
        return None
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_DIR / f"img-{int(time.time() * 1000)}.jpg"
    try:
        target.write_bytes(small)
    except Exception:
        return None
    return target


def to_data_url(path: Path) -> str:
    """转成 data URL，给模型当 image_url 用。"""
    try:
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return ""


def parse(path: Path) -> tuple[str, str]:
    """返回 (正文, 备注)。正文会先过一遍 sanitize，去掉隐形字符。"""
    text, note = _parse_raw(path)
    return sanitize(text), note


def _parse_raw(path: Path) -> tuple[str, str]:
    """按扩展名解析。返回值未经消毒，调用方走 parse() 就行。"""
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            raw = path.read_bytes()[:300_000]
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc)[:MAX_TEXT], ""
                except Exception:
                    continue
            # latin-1 永远能解码成功，所以必须校验可读性：
            # 否则二进制文件会被当成乱码文本喂给模型，答得一本正经但全是废话。
            try:
                t = raw.decode("latin-1")
                sample = t[:2000]
                printable = sum(1 for ch in sample if ch.isprintable() or ch in "\n\r\t")
                if sample and printable / len(sample) > 0.9:
                    return t[:MAX_TEXT], ""
            except Exception:
                pass
            return "", "解不出来，可能是二进制文件"

        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            parts = []
            for page in reader.pages[:30]:
                parts.append(page.extract_text() or "")
            return "\n".join(parts)[:MAX_TEXT], f"{len(reader.pages)} 页"

        if ext in (".docx", ".doc"):
            if ext == ".doc":
                return "", "旧版 .doc 暂不支持，另存为 .docx"
            import docx
            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for table in d.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
            return "\n".join(parts)[:MAX_TEXT], ""

        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets[:5]:
                parts.append(f"## {ws.title}")
                for row in ws.iter_rows(max_row=200, values_only=True):
                    vals = [str(v) for v in row if v is not None]
                    if vals:
                        parts.append(" | ".join(vals))
            return "\n".join(parts)[:MAX_TEXT], ""

        if ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(str(path))
            parts = []
            for i, slide in enumerate(prs.slides[:30]):
                texts = [sh.text.strip() for sh in slide.shapes
                         if hasattr(sh, "text") and sh.text and sh.text.strip()]
                if texts:
                    parts.append(f"## 第{i+1}页\n" + "\n".join(texts))
            return "\n".join(parts)[:MAX_TEXT], ""

    except Exception as e:  # noqa: BLE001
        return "", f"解析失败：{type(e).__name__}"

    return "", f"不支持的格式 {ext or '（无扩展名）'}"


# ---------- 「刚收到的文件」：给 /task 和 agent_task 用 ----------
def remember_pending(src: Path, name: str = "") -> Path | None:
    """把刚收到的文件登记成「下一条指令可以带上它」，并在 agentd 的 inbox 里留一份。

    只占一份磁盘：优先**硬链接**（同一块盘，不复制数据），失败再复制。
    agentd 取文件是搬进 /work/<id>/，不会原地改，所以硬链接是安全的。

    这一步以前是 plugins/agent 自己重新下载一遍完成的——同一份文件下两次、
    361KB 的 PDF 白白多走一趟网络。现在由收到文件的那一方（chat）登记，
    agent 只读登记表。
    """
    try:
        AGENT_INBOX.mkdir(parents=True, exist_ok=True)
        target = AGENT_INBOX / f"{int(time.time())}-{_safe_name(name or src.name)}"
        if not target.exists():
            try:
                os.link(src, target)
            except OSError:
                shutil.copy2(src, target)
        PENDING_JSON.parent.mkdir(parents=True, exist_ok=True)
        PENDING_JSON.write_text(json.dumps(
            {"path": str(target), "name": name or src.name, "ts": time.time()},
            ensure_ascii=False), encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001
        return None


def pending(ttl: float = PENDING_TTL) -> dict | None:
    """最近登记的文件；超过 ttl 就当没发过。"""
    try:
        info = json.loads(PENDING_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not info.get("path") or time.time() - float(info.get("ts") or 0) > ttl:
        return None
    return info


def clear_pending() -> None:
    try:
        PENDING_JSON.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def cleanup_inbox(max_age_hours: float = 24, now: float | None = None) -> dict:
    """删掉 agentd 收件箱里过期的文件。

    `PENDING_TTL`（30 分钟）只管「登记表还算不算数」，**文件本身没人删**：
    实测一天就攒了四个（12:19 的 docx、14:13 的两个 PDF），
    而 /opt/agentd 和 bot 的数据在同一个盘上，放着只会越堆越多。

    两条保护：正在等着被派活的那份不删（他可能隔了半小时才说要干什么）；
    只按修改时间判断，不猜内容。
    """
    now = time.time() if now is None else now
    cutoff = now - max_age_hours * 3600
    keep: Path | None = None
    info = pending()
    if info:
        try:
            keep = Path(info["path"])
        except Exception:  # noqa: BLE001
            keep = None
    removed, freed = 0, 0
    try:
        for p in AGENT_INBOX.glob("*"):
            try:
                if not p.is_file() or keep == p or p.stat().st_mtime > cutoff:
                    continue
                size = p.stat().st_size
                p.unlink()
                removed += 1
                freed += size
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return {"removed": removed, "freed": freed}
