"""物理删除：只清**知识库**的废弃页 + 过期的代码快照。

## 为什么只动这两样

实测（2026-11-03）：磁盘 40G 用 12G，知识页 12 个共 52K。真正的增长点是
`/opt/backup` 里每次部署留的 `code-*.tgz`（一个 300K，已经二十多份）。

而知识页一年哪怕写 500 页也才 2MB——**它永远不会是存储问题**。所以这里的删除
不是为了腾空间，是为了让「已废弃」真的消失，别让下架变成摆设。

## 边界（用户 2026-11-03 明确）

- 只清知识库：`learn/notes` 里**已废弃 ≥180 天**的页。
- **不碰记忆库**：`livingmemory.db`、`learn/raw/`（对话原文）都不在此列。
- 删除前必须打包 + 校验，删完留记录。

## 执行协议

    选候选 → 打包到 /opt/backup/purged-knowledge-*.tgz → 校验能列出全部成员
    → 删文件 → 删 wiki_pages 行 → 写日志

任何一步失败就中止，不删。
"""
from __future__ import annotations

import logging
import tarfile
import time
from pathlib import Path

log = logging.getLogger("qqbot.purge")

BACKUP_DIR = Path("/opt/backup")
PURGE_KEEP_PACKS = 2      # 保留最近几份清理包


def select_candidates(pages: list[dict], now: float | None = None,
                      days: float = 180) -> list[dict]:
    """挑出该物理删除的知识页。

    三个条件同时满足才删：
      1. 状态已是「已废弃」（先下架，再谈清理）
      2. 废弃已经超过 days 天（mtime 就是归档那一刻，set_meta 会重写文件）
      3. 从没被验证过、也从没被打开过 —— 真正没人要的

    验证过的页永久保留：它可能是对的，只是暂时用不上。
    """
    now = now or time.time()
    out = []
    for p in pages:
        if p.get("status") != "已废弃":
            continue
        mt = float(p.get("mtime") or 0)
        if not mt or (now - mt) / 86400 < days:
            continue
        if float(p.get("verified_ts") or 0):
            continue
        if float(p.get("last_open_ts") or 0):
            continue
        out.append(p)
    out.sort(key=lambda p: float(p.get("mtime") or 0))
    return out


def pack(paths: list[Path], tag: str = "knowledge") -> Path | None:
    """打包并校验。返回包路径；失败返回 None（调用方据此中止删除）。"""
    if not paths:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"purged-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.tgz"
    try:
        with tarfile.open(dest, "w:gz") as tf:
            for p in paths:
                if p.exists():
                    tf.add(str(p), arcname=p.name)
        # 校验：能打开、成员数对得上，才算写成功
        with tarfile.open(dest, "r:gz") as tf:
            names = tf.getnames()
        if len(names) < len([p for p in paths if p.exists()]):
            log.error("清理包成员数不符：%s < %s", len(names), len(paths))
            dest.unlink(missing_ok=True)
            return None
        return dest
    except Exception as e:  # noqa: BLE001
        log.error("打包失败，放弃删除：%s", e)
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def rotate(prefixes=("code-", "config-", "persona-"), keep: int = 10) -> list[str]:
    """备份快照轮转：每类只留最近 keep 份，其余删除。

    这些是部署时自动留的，重来一次就有，删了没有信息损失。
    """
    removed = []
    try:
        entries = [p for p in BACKUP_DIR.iterdir() if p.is_file()]
    except Exception:
        return removed
    for pref in prefixes:
        group = sorted((p for p in entries if p.name.startswith(pref)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in group[keep:]:
            try:
                p.unlink()
                removed.append(p.name)
            except Exception:
                pass
    return removed


def prune_old_packs(keep: int = PURGE_KEEP_PACKS) -> list[str]:
    """清理包只留最近几份（内容已在里面，重复留着没意义）。"""
    removed = []
    try:
        packs = sorted(BACKUP_DIR.glob("purged-*.tgz"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return removed
    for p in packs[keep:]:
        try:
            p.unlink()
            removed.append(p.name)
        except Exception:
            pass
    return removed


def purge_knowledge(days: float = 180, max_files: int = 20) -> dict:
    """执行知识页物理删除。返回 {packed, deleted, tarball, titles}。"""
    from app_core import wiki

    result = {"packed": 0, "deleted": 0, "tarball": "", "titles": []}
    cands = select_candidates(wiki.pages(), days=days)[:max(0, int(max_files))]
    if not cands:
        return result
    files = [Path(p["path"]) for p in cands]
    pack_path = pack(files, "knowledge")
    if pack_path is None:
        log.error("知识页打包失败，本次不删除")
        return result
    result["tarball"] = str(pack_path)
    result["packed"] = len(files)

    deleted = []
    for p in cands:
        try:
            Path(p["path"]).unlink()
            deleted.append(p)
        except Exception as e:  # noqa: BLE001
            log.warning("删除失败 %s：%s", p["path"], e)
    if deleted:
        try:
            c = wiki._conn()
            with c:
                for p in deleted:
                    c.execute("DELETE FROM wiki_pages WHERE path=?", (p["path"],))
            c.close()
        except Exception as e:  # noqa: BLE001
            log.warning("清 wiki_pages 行失败：%s", e)
    result["deleted"] = len(deleted)
    result["titles"] = [(p.get("title") or "")[:14] for p in deleted]
    log.info("知识页物理删除 %d 个（打包 %s）", len(deleted), pack_path.name)
    return result
