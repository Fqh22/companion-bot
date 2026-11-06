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

    # ---- 人格 / 话题 ----
    data_dir = TMP / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    owner = data_dir / "persona.json"
    owner.write_text(json.dumps({
        "system_prompt": "旧人格", "begin_dialogs": ["在吗", "在"],
        "id": "keep-me"}, ensure_ascii=False), encoding="utf-8")
    stranger = data_dir / "persona-stranger.json"
    stranger.write_text(json.dumps({
        "system_prompt": "外人版", "begin_dialogs": [],
        "title": "小鲸鱼"}, ensure_ascii=False), encoding="utf-8")
    watch = data_dir / "watchlist.json"
    watch.write_text(json.dumps([
        {"topic": "A", "last": "旧结论", "ts": 1},
        {"topic": "B", "last": "B结论", "ts": 2},
    ], ensure_ascii=False), encoding="utf-8")

    pv, pe = mod.validate_persona({"system_prompt": "x", "begin_dialogs": ["a", "b"]})
    check("合法人格通过", not pe, str(pe))
    _, pe = mod.validate_persona({"system_prompt": "x", "begin_dialogs": ["a"]})
    check("人格对话必须成对", bool(pe), str(pe))
    _, pe = mod.validate_persona({"system_prompt": "", "begin_dialogs": []})
    check("人格不能为空", bool(pe), str(pe))
    merged = mod.merge_watch([{"topic": "A", "last": "旧", "ts": 1}], ["A", "C"])
    check("话题保留同名基线", merged[0]["last"] == "旧" and merged[1]["topic"] == "C"
          and merged[1]["last"] == "", str(merged))

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

        def post(path, obj):
            req = urllib.request.Request(base + path, method="POST",
                                         data=json.dumps(obj).encode(),
                                         headers={"X-Token": "test-token",
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        req = urllib.request.Request(base + "/api/assets", headers={"X-Token": "test-token"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assets = json.loads(r.read())
        check("读到两个人格", len(assets.get("personas") or []) == 2, str(len(assets.get("personas") or [])))
        check("读到巡检基线", len((assets.get("watchlist") or {}).get("topics") or []) == 2)

        p0 = assets["personas"][0]
        st, d = post("/api/persona", {"which": p0["which"],
                                      "system_prompt": p0["system_prompt"],
                                      "begin_dialogs": p0["begin_dialogs"],
                                      "dry_run": True})
        check("人格内容没变不算改动", st == 200 and d.get("changed") is False, str(d)[:80])
        w0 = assets["watchlist"]["topics"]
        st, d = post("/api/watchlist", {"topics": [x["topic"] for x in w0], "dry_run": True})
        check("话题内容没变不算改动", st == 200 and d.get("changed") is False, str(d)[:80])

        st, d = post("/api/persona", {"which": "owner", "system_prompt": "新人格",
                                      "begin_dialogs": ["在吗", "在的"], "dry_run": True})
        check("人格干跑出 diff",
              st == 200 and "新人格" in (d.get("diff") or ""),
              str(d.get("diff") or "")[:80])
        st, d = post("/api/persona", {"which": "owner", "system_prompt": "新人格",
                                      "begin_dialogs": ["在吗", "在的"]})
        saved = json.loads(owner.read_text(encoding="utf-8"))
        check("人格保存成功", st == 200 and d.get("ok") is True, str(d))
        check("人格未知字段保留", saved.get("id") == "keep-me", str(saved.get("id")))
        check("人格备份已生成", bool(d.get("backup")) and Path(d["backup"]).exists(), d.get("backup"))

        st, d = post("/api/persona", {"which": "owner", "system_prompt": "x",
                                      "begin_dialogs": ["只有一条"]})
        check("人格奇数对话被拒", st == 400, str(d))

        st, d = post("/api/watchlist", {"topics": ["A", "C"], "dry_run": True})
        check("话题干跑出 diff", st == 200 and d.get("ok") is True, str(d)[:80])
        st, d = post("/api/watchlist", {"topics": ["A", "C"]})
        wl = json.loads(watch.read_text(encoding="utf-8"))
        check("话题保存成功", st == 200 and d.get("ok") is True, str(d)[:80])
        check("话题保留 A 的基线", wl[0]["topic"] == "A" and wl[0]["last"] == "旧结论", str(wl[0]))
        check("新话题 C 为空基线", wl[1]["topic"] == "C" and wl[1]["last"] == "", str(wl[1]))
        check("删掉的 B 不再写回", all(x["topic"] != "B" for x in wl), str(wl))
        check("话题免重启标记", d.get("hot_reload") is True, str(d))
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)

    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
