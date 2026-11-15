"""历史主题词重抽：让老记忆也换上新词表。

**状态**：一次性迁移脚本（2026-11-05 用于重抽 119 篇老记忆的主题），保留下来
供审计/重跑；默认 dry-run，加 --apply 才写库（写前自动备份 livingmemory.db）。

**为什么需要**：2026-11-05 加了「生成时复用词表」之后，只有**新**记忆会受
约束；库里的 119 篇老记忆还带着旧标签（410 个里 393 个只出现过一次）。
不重抽的话，实体连边要等新数据慢慢稀释，几个月都看不出效果。

**只重抽 topics，不动 summary / key_facts / importance**：那几项是当时对话
的真实产物，重抽会把内容也一起改写，风险和收益都不对等。这里只问模型
「这段记忆该打哪几个标签」，并明确要求优先复用已有词表。

**默认 dry-run**，只打印会改什么；`--apply` 才写库，写之前自动备份一份
`/opt/backup/livingmemory-<时间>.db`（写不进去就退到 /tmp）。

**温度**：这里显式传 `temperature=0.2`，只有这个脚本用；线上摘要仍走聊天档
1.15。原因是不压温度的话「同一段输入重跑两次」标签会变一大半（实测 8/10），
迁移就没法验证也没法重来。用 `--temp` 可以改。

用法：
    python retopics.py                 # 看会改成什么样，不写
    python retopics.py --apply         # 真写（先备份）
    python retopics.py --apply --limit 20
    python retopics.py --temp 0        # 完全贪心，用来验证收敛性
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/nonebot")

DB = Path("/opt/qqbot-data/livingmemory.db")
BACKUP_DIR = Path("/opt/backup")
# 迁移期间固定用这一份词表。不固定的话每次跑都会按「上一轮结果」重算 Top50，
# 提示变了输出就跟着变——实测重跑 119 篇里 72 篇又被改了一遍，永远收敛不了。
VOCAB_FILE = Path("/opt/nonebot/data/topic-vocab.txt")

SYSTEM = (
    "你给记忆打主题标签。只输出 JSON，不要解释：\n"
    '{"topics": ["...", "..."]}\n'
    "2–4 个主题词，每个 2–6 个字，用来把「同一件事的记忆」归到一起。"
)


def load_docs(c) -> list[tuple[int, str, str, dict]]:
    rows = c.execute("SELECT id, doc_id, text, metadata FROM documents").fetchall()
    out = []
    for did, doc_id, text, meta in rows:
        try:
            m = json.loads(meta or "{}")
        except Exception:
            m = {}
        if m.get("status") == "archived":
            continue
        out.append((did, doc_id, text or "", m))
    return out


async def ask_topics(core, text: str, hint: str, temperature: float = 0.2) -> list[str]:
    from app_core import summarize as summarize_mod

    raw = await core.complete_summary(
        [{"role": "system", "content": SYSTEM + ("\n\n" + hint if hint else "")},
         {"role": "user", "content": f"【这段记忆】\n{text[:600]}\n\n给它打标签。"}],
        max_tokens=120, temperature=temperature)
    data = summarize_mod.parse(raw)
    return data.get("topics") or []


def backup_db(c) -> Path:
    """用 SQLite 的 backup API 备份，不能直接拷贝文件。

    库是 WAL 模式：正在写入时 `.db` 里可能还是旧页，真数据在 `-wal` 里。
    copy2 出来的副本会缺最后一段，回滚过去等于丢记忆。
    `/opt/backup` 是 root 的，脚本以 ubuntu 跑写不进去，退到 /tmp 并提示。
    """
    for base in (BACKUP_DIR, Path("/tmp")):
        dst = base / f"livingmemory-{time.strftime('%Y%m%d-%H%M%S')}.db"
        try:
            con = sqlite3.connect(str(dst))
            with con:
                c.backup(con)
            con.close()
            if base != BACKUP_DIR:
                print(f"⚠ /opt/backup 不可写，备份落在 {dst}（记得挪走）")
            return dst
        except Exception:  # noqa: BLE001  权限不足时 sqlite 抛的是 OperationalError
            try:
                dst.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            continue
    raise SystemExit("备份失败，放弃写入")


def load_vocab(path: Path, current: list[str]) -> tuple[list[str], bool]:
    """读固定词表；没有就把这一次算出来的写下来，之后一直用它。

    迁移要能重跑，前提是**输入不变**。词表跟着库走的话，每次跑完 Top50 都会
    变一点点，提示一变输出就变，看起来就像"永远收敛不了"。
    """
    if path.exists():
        items = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        if items:
            return items, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(current), encoding="utf-8")
    return current, True


async def main() -> None:
    from app_core import vocab as vocab_mod
    from app_core.chat import ChatCore

    apply = "--apply" in sys.argv
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    # 压低采样温度让「同一段输入重跑结果一致」。线上摘要仍用聊天档 1.15，
    # 不改它——纪要文本直接进 prompt，语气是调过的，动温度等于动语气。
    temp = 0.2
    if "--temp" in sys.argv:
        temp = float(sys.argv[sys.argv.index("--temp") + 1])

    c = sqlite3.connect(DB)
    docs = load_docs(c)
    if limit:
        docs = docs[:limit]
    vocab, fresh = load_vocab(VOCAB_FILE, vocab_mod.build(
        [m.get("topics") for _, _, _, m in docs], 50))
    hint = ""
    from app_core import summarize as summarize_mod

    hint = summarize_mod.vocab_hint(vocab)
    print(f"文档 {len(docs)} 篇；词表 {len(vocab)} 个"
          f"（{'本次新建，已固定到 ' + str(VOCAB_FILE) if fresh else '沿用 ' + str(VOCAB_FILE)}）")
    print(f"前 10：{'、'.join(vocab[:10])}")
    print(f"模式：{'APPLY（会写库）' if apply else 'dry-run（只看不改）'}  温度：{temp}\n")

    if apply:
        dst = backup_db(c)
        print(f"已备份：{dst}\n")

    core = ChatCore()
    changed = skipped = failed = 0
    before_total = set()
    after_total = set()
    report = []
    for i, (did, doc_id, text, meta) in enumerate(docs, 1):
        old = [str(x) for x in (meta.get("topics") or [])]
        before_total.update(old)
        try:
            new = await ask_topics(core, text, hint, temperature=temp)
            new = vocab_mod.canonicalize(new, vocab + old) or old
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(docs)}] 失败 {type(e).__name__}: {e}")
            continue
        after_total.update(new)
        if sorted(new) == sorted(old):
            skipped += 1
            continue
        changed += 1
        print(f"  [{i}/{len(docs)}] {old} → {new}")
        report.append({"doc_id": doc_id, "old": old, "new": new})
        if apply:
            meta["topics"] = new
            c.execute("UPDATE documents SET metadata=? WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False), did))
            c.execute("UPDATE memory_atoms SET entities=? WHERE parent_memory_id=?",
                      (json.dumps(new, ensure_ascii=False), did))
    if apply:
        c.commit()
    c.close()

    print(f"\n=== 改动 {changed} / 未变 {skipped} / 失败 {failed} ===")
    print(f"标签总数：改前 {len(before_total)} → 改后 {len(after_total)}")
    out = Path("/tmp/retopics-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细：{out}")
    if not apply:
        print("\n（dry-run，没有写库；加 --apply 才生效）")


asyncio.run(main())
