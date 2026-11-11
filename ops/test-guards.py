"""护栏自检：故意写违规代码，确认 lint 真的会红。

为什么要这个：规则最怕「悄悄失效」。改了正则、换了目录、重构了扫描逻辑，
lint 可能永远 PASS——那就等于没有护栏。
这里给每条关键规则喂一个反例，必须 FAIL；跑完清理临时文件。

不联网、不碰生产库；约 1 秒。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
# 两种部署布局：仓库里 ops 在 CODE.parent，云端 /opt/nonebot 下 ops 与代码同级
OPS = CODE / "ops"
if not (OPS / "lint-plugins.py").is_file():
    OPS = CODE.parent / "ops"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    lp = _load("lint_plugins_guard", OPS / "lint-plugins.py")
    ll = _load("lint_layers_guard", OPS / "lint-layers.py")
    tmp = Path(tempfile.mkdtemp(prefix="guards-"))
    try:
        bad = tmp / "bad.py"
        bad.write_text("import nonebot\nprint('x')\n", encoding="utf-8")
        check("抓 app_core import nonebot", bool(lp.check_no_nonebot(bad)))
        check("抓裸 print", bool(lp.check_no_print(bad)))

        old_core, old_plugins = lp.CORE, lp.PLUGINS
        lp.CORE, lp.PLUGINS = tmp, tmp
        try:
            hp = tmp / "hp.py"
            hp.write_text('DB = "/opt/nonebot/data/x.db"\n', encoding="utf-8")
            check("抓硬编码 /opt", bool(lp.check_no_hardcoded_paths()))
        finally:
            lp.CORE, lp.PLUGINS = old_core, old_plugins

        old_ll = ll.CORE
        ll.CORE = tmp
        try:
            (tmp / "pacing.py").write_text("from app_core import runner\n", encoding="utf-8")
            ll.FAILS.clear()
            ll.check_domain_layers()
            hit = any("domain 不得 import 编排层" in x for x in ll.FAILS)
            check("抓 domain 反向依赖", hit, str(ll.FAILS)[:60])
        finally:
            ll.FAILS.clear()
            ll.CORE = old_ll
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
