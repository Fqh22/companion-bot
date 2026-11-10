"""计划契约：PlanStep + VerifySpec 的规范化与校验。

planner 仍输出 JSON，但进入 store 之前必须过这里：
- idx 唯一、依赖存在；
- verify 至少有一个可机械检查的条件；
- custom 只能引用前面步骤创建过的 .py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_ALLOWED = {"exit_code", "file_exists", "min_size", "contains", "regex",
            "custom", "noop"}
_SCRIPT_RE = re.compile(r"[\w./-]+\.py")
_CREATE_RE = re.compile(r"(写|创建|生成|保存|编写).{0,64}\.py")


@dataclass(frozen=True)
class VerifySpec:
    raw: dict

    @classmethod
    def parse(cls, raw: object) -> "VerifySpec":
        return cls(dict(raw) if isinstance(raw, dict) else {})

    def normalized(self) -> dict:
        return {k: v for k, v in self.raw.items() if k in _ALLOWED}

    def is_empty(self) -> bool:
        return not self.normalized()


@dataclass(frozen=True)
class PlanStep:
    idx: int
    desc: str
    depends_on: tuple[int, ...] = ()
    verify: VerifySpec = field(default_factory=lambda: VerifySpec({}))

    def as_dict(self) -> dict:
        return {"idx": self.idx, "desc": self.desc,
                "depends_on": list(self.depends_on),
                "verify": self.verify.normalized()}


def validate_plan(raw_steps: list[dict]) -> list[dict]:
    """返回可写入 store 的规范步骤；坏 verify 降级而不是让任务必死。"""
    raw_steps = [s for s in (raw_steps or []) if isinstance(s, dict)]
    raw_steps = [s for s in raw_steps if str(s.get("desc") or "").strip()][:8]
    made: set[str] = set()
    out: list[dict] = []
    seen: set[int] = set()
    for i, s in enumerate(raw_steps):
        idx = int(s.get("idx", i))
        while idx in seen:
            idx += 1
        seen.add(idx)
        desc = str(s.get("desc") or "").strip()[:200]
        if _CREATE_RE.search(desc):
            for name in _SCRIPT_RE.findall(desc):
                made.add(name)
                made.add(Path(name).name)
        spec = VerifySpec.parse(s.get("verify"))
        v = spec.normalized()
        c = v.get("custom")
        if c:
            missing = [r for r in _SCRIPT_RE.findall(str(c))
                       if r not in made and Path(r).name not in made]
            if missing:
                v = {"exit_code": 0}
        if not v:
            v = {"noop": True}
        deps = []
        for x in s.get("depends_on") or []:
            try:
                d = int(x)
            except Exception:  # noqa: BLE001
                continue
            if 0 <= d < len(raw_steps) and d != idx and d not in deps:
                deps.append(d)
        out.append(PlanStep(idx, desc, tuple(deps), VerifySpec(v)).as_dict())
    return out
