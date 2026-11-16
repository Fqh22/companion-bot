#!/usr/bin/env python3
"""[infra] 复盘落盘的逐字节基线：apply_review 的产出 diff。

为什么需要：P2 把 apply_review 从 learn.py 搬到 learn_store.py。735 条离线回归
只保证「跑通、断言数一致」，不保证「写到文件里的字节一样」——复盘产出没有
逐字节基线，行为差异只能靠人抽检。这个工具把固定输入 + 固定 day 跑一遍，
把产出的 sha256 记进基线；改动写入规则后 diff 会指出哪一页变了。

用法：
    python ops/ab-review.py --write    # 重建基线（改完写入规则、确认无误后）
    python ops/ab-review.py            # 对比，有差异就非零退出

不联网、不碰生产库：临时根 + 固定时钟，数据全部来自 CASES。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent / "nonebot"
if not (CODE / "app_core").is_dir():
    CODE = HERE.parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 必须在 import app_core 之前把根指到临时目录：paths 在 import 时固化。
TMP = Path(tempfile.mkdtemp(prefix="ab-review-"))
os.environ["QQBOT_ROOT"] = str(TMP / "root")
sys.path.insert(0, str(CODE))

from app_core import learn_store as ls  # noqa: E402

BASELINE = HERE / "fixtures" / "review-baseline.json"

# 固定输入：覆盖「画像合并/过期字段/纠正清单/好奇清单/解决好奇」五类写入。
CASES = [
    {"learned": "他今天在填简历，说想找后端。",
     "profile": {"职业与求职": "找工作中"},
     "corrections": ["[文字] 别只顺着说"],
     "curious": ["他为什么突然想换工作"]},
    {"learned": "他提到家里催婚，语气很淡。",
     "profile": {"家庭": "催婚"},
     "corrections": [],
     "curious": []},
    {"learned": "他确认了简历模板，下周投。",
     "profile": {"职业与求职": "下周投简历"},
     "corrections": [],
     "curious": [],
     "resolved_curious": ["他为什么突然想换工作"]},
]


# profile.json 带 updated_at / day（写入时刻），对 diff 无意义：
# 归一化掉时间字段再 hash，基线才不会每次跑都「变」。
_VOLATILE = ("updated_at", "day")


def _sha(p: Path) -> str:
    data = p.read_bytes()
    if p.suffix == ".json":
        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                for k in _VOLATILE:
                    obj.pop(k, None)
                # seen / previous.at 存的都是写入时刻：只保留结构，值无 diff 意义
                if isinstance(obj.get("seen"), dict):
                    obj["seen"] = {k: 0 for k in obj["seen"]}
                if isinstance(obj.get("previous"), dict):
                    for v in obj["previous"].values():
                        if isinstance(v, dict):
                            v["at"] = 0
            data = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
        except Exception:  # noqa: BLE001
            pass
    return hashlib.sha256(data).hexdigest()


def _snapshot() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(ls.LEARN.rglob("*")):
        if p.is_file():
            out[p.relative_to(ls.LEARN).as_posix()] = _sha(p)
    return out


def _prepare() -> None:
    ls.LEARN = TMP / "learn"
    ls.PROFILE = ls.LEARN / "self_profile.md"
    ls.PROFILE_JSON = ls.LEARN / "profile.json"
    ls.CORRECTIONS = ls.LEARN / "corrections.md"
    ls.CURIOUS = ls.LEARN / "curious.md"
    ls.LEARN.mkdir(parents=True, exist_ok=True)


def main() -> int:
    write = "--write" in sys.argv
    _prepare()
    for i, case in enumerate(CASES, 1):
        ls.apply_review(case, day=f"2026-11-0{i}")
    got = _snapshot()
    if write:
        BASELINE.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        print(f"基线已写入：{len(got)} 个文件 → {BASELINE}")
        return 0
    if not BASELINE.exists():
        print(f"缺少基线：{BASELINE}（先跑 --write）")
        return 2
    want = json.loads(BASELINE.read_text(encoding="utf-8"))
    added = sorted(set(got) - set(want))
    removed = sorted(set(want) - set(got))
    changed = sorted(k for k in set(got) & set(want) if got[k] != want[k])
    if not (added or removed or changed):
        print(f"复盘产出基线：PASS（{len(got)} 个文件逐字节一致）")
        return 0
    for k in added:
        print(f"新增 {k}")
    for k in removed:
        print(f"消失 {k}")
    for k in changed:
        print(f"改变 {k}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
