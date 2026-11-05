"""验收条件求值（§七 verify DSL）。

每步必须带一个验收条件；没有验收条件的步骤只算“模型认为完成了”。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Observation:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    files: dict[str, int] = field(default_factory=dict)   # 输出提到的文件 → 大小
    note: str = ""


def evaluate(spec: dict, obs: Observation, runner=None,
             resolver=None) -> tuple[bool, str, bool]:
    """返回 (通过?, 原因, 是否未验证)。

    runner(cmd) -> (exit_code, stdout) 只在 {"custom": ...} 时用到。
    resolver(path) -> size|None 用来核对**真实文件**（不能只信工具上报：
    模型可能用 shell 创建了文件，而 shell 不上报文件列表）。
    """
    if not spec:
        return False, "没有验收条件", True

    if spec.get("noop"):
        return True, "无法机械校验（标记未验证）", True

    checks: list[tuple[bool, str]] = []

    if "exit_code" in spec:
        want = int(spec["exit_code"])
        checks.append((obs.exit_code == want, f"退出码 {obs.exit_code}≠{want}"))

    if "file_exists" in spec:
        name = str(spec["file_exists"])
        size = obs.files.get(name)
        if size is None and resolver is not None:
            size = resolver(name)
        if size is None:
            checks.append((False, f"文件不存在：{name}"))
        else:
            min_size = int(spec.get("min_size", 1))
            checks.append((size >= min_size, f"{name} 大小 {size}<{min_size}"))

    if "contains" in spec:
        needle = str(spec["contains"])
        hay = (obs.stdout or "") + (obs.stderr or "")
        checks.append((needle in hay, f"输出不含「{needle}」"))

    if "regex" in spec:
        pat = str(spec["regex"])
        hay = (obs.stdout or "") + (obs.stderr or "")
        checks.append((bool(re.search(pat, hay)), f"输出不匹配 /{pat}/"))

    if "custom" in spec:
        # 自定义检查脚本必须**真的在沙箱里执行**——只看上一步的退出码等于没验。
        if runner is None:
            checks.append((False, "custom 验收需要 runner，但未提供"))
        else:
            code, out = runner(spec)
            checks.append((code == 0, f"自定义检查失败（{code}）：{str(out)[:120]}"))

    if not checks:
        return False, f"未知的验收条件：{spec}", True

    failed = [why for ok, why in checks if not ok]
    if failed:
        return False, "；".join(failed), False
    return True, "通过", False
