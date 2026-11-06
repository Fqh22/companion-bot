#!/usr/bin/env python3
"""配置面板的离线测试：不联网、不碰生产配置。

跑法：
    python ops/test-config-ui.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
MOD_PATH = HERE / "config-ui.py"
TMP = Path(tempfile.mkdtemp(prefix="config-ui-test-"))
PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}" + (f" | {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f" | {extra}" if extra else ""))


def load_mod():
    spec = importlib.util.spec_from_file_location("config_ui", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod      # dataclass 需要模块已登记，否则 __module__ 查不到
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load_mod()

    # ---- schema ----
    schema = mod.build_schema()
    by_id = {f["id"]: f for g in schema for f in g["fields"]}
    check("schema 有管理员", "admin.user_id" in by_id)
    check("schema 有嵌套计费", "pricing.deepseek-flash.in" in by_id)
    check("schema 有列表字段", by_id["group.whitelist"]["type"] == "list")
    check("schema 分组顺序保留", schema[0]["id"] == "admin" and schema[-1]["id"] == "pricing")

    cfg = TMP / "config.yaml"
    cfg.write_text(
        "# 顶部注释\n"
        "admin:\n"
        "  user_id: \"123456\"\n"
        "llm:\n"
        "  temperature: 1.15\n"
        "  model: deepseek-flash\n"
        "custom_key: hello\n",
        encoding="utf-8")
    backup_dir = TMP / "backups"
    old = cfg.read_text(encoding="utf-8")

    doc = mod.load_doc(cfg)
    vals = mod.collect_values(doc)
    check("缺省字段回落默认值", vals["llm.max_tokens"] == 1000, str(vals["llm.max_tokens"]))
    check("已有字段读原值", vals["llm.temperature"] == 1.15)

    payload = {"llm.temperature": 1.25, "group.whitelist": [123, 456]}
    validated, errors = mod.validate_payload(payload)
    check("合法值通过校验", not errors, str(errors))
    mod.apply_values(doc, validated)
    new = mod.dump_doc(doc)
    check("保存保留顶部注释", "# 顶部注释" in new, new[:20])
    check("保存保留未知键", "custom_key: hello" in new)
    check("保存写入新值", "temperature: 1.25" in new)
    check("diff 有增删行", "-  temperature: 1.15" in mod.make_diff(old, new)
          and "+  temperature: 1.25" in mod.make_diff(old, new))

    backup = mod.write_config(cfg, backup_dir, old, new)
    check("保存前有备份", bool(backup) and Path(backup).exists(), backup)
    check("备份内容是旧文件", Path(backup).read_text(encoding="utf-8") == old)
    check("文件已写入", "temperature: 1.25" in cfg.read_text(encoding="utf-8"))

    # ---- 非法值 ----
    bad, errs = mod.validate_payload({"llm.temperature": "hot"})
    check("非数字温度被拒", bool(errs) and not bad, str(errs))
    bad, errs = mod.validate_payload({"llm.max_tokens": 99999})
    check("超范围被拒", bool(errs), str(errs))
    bad, errs = mod.validate_payload({"profile.inject": "maybe"})
    check("非法枚举被拒", bool(errs), str(errs))
    bad, errs = mod.validate_payload({"group.whitelist": ["abc"]})
    check("群号非整数被拒", bool(errs), str(errs))
    bad, errs = mod.validate_payload({"nope.nope": 1})
    check("未知字段被拒", bool(errs), str(errs))

    # ---- unknown ----
    doc2 = mod.load_doc(cfg)
    mod.deep_set(doc2, ["custom2", "x"], 1)
    unknown = {u["id"] for u in mod.unknown_fields(doc2)}
    check("能识别未知键", "custom2.x" in unknown, str(unknown))

    # ---- HTTP 鉴权 + 干跑 ----
    app = mod.App(config_path=cfg, backup_dir=backup_dir, token="test-token", port=0)
    mod.Handler.app = app
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    port = srv.server_address[1]
    app.port = port
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(base + "/api/config")
        try:
            urllib.request.urlopen(req, timeout=5)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("无 token 返回 401", code == 401, str(code))

        req = urllib.request.Request(base + "/api/config",
                                     headers={"X-Token": "test-token"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        check("带 token 能读配置", body.get("ok") is True and body.get("path") == str(cfg))
        check("返回带 schema 分组", len(body.get("groups") or []) > 10)

        post = json.dumps({"values": {"llm.temperature": 1.3}, "dry_run": True}).encode()
        req = urllib.request.Request(base + "/api/config", data=post, method="POST",
                                     headers={"X-Token": "test-token",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        check("干跑返回 diff", body.get("ok") is True and "+  temperature: 1.3" in (body.get("diff") or ""))
        check("干跑不落盘", "temperature: 1.25" in cfg.read_text(encoding="utf-8"))
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
