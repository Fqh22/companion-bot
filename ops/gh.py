#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 只读工具：拉 CI 日志、读任意 commit 的文件、列最近 run。

为什么需要：CI 红了但本地/云端都绿时，唯一能定位的是 job 日志。
浏览器里看要人工截图，这里一条命令拉下来。

token 从 /opt/qqbot-data/keys.json 的 github 字段读——不进仓库。

用法：
    python ops/gh.py runs [n]            # 最近 n 次 run 的状态
    python ops/gh.py logs <sha> [job]    # 某次 run 的失败 step 全文
    python ops/gh.py file <path> [rev]   # 读某个 commit 里的文件
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Fqh22/companion-bot"
API = "https://api.github.com/repos/" + REPO


def _tok() -> str:
    for p in (Path("/opt/qqbot-data/keys.json"), Path.home() / ".config/qqbot/keys.json"):
        try:
            return str(json.loads(p.read_text(encoding="utf-8")).get("github") or "")
        except Exception:
            continue
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：job logs 会 302 到 S3，带 Authorization 去请求
    S3 会因签名冲突返回 403（今天踩过）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url: str, raw: bool = False):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + _tok(),
        "Accept": "application/vnd.github+json",
        "User-Agent": "qqbot-ops",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=40) as r:
            body = r.read()
            loc = r.headers.get("Location")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location")
            body = b""
        else:
            raise
    if loc:
        # S3 预签名 URL：**不能带 Authorization**，否则签名校验失败
        with urllib.request.urlopen(loc, timeout=60) as r2:
            body = r2.read()
    if raw:
        return body.decode("utf-8", errors="replace")
    return json.loads(body.decode("utf-8", errors="replace"))


def cmd_runs(n: int = 5) -> int:
    j = _get(API + "/actions/runs?per_page=" + str(n))
    for r in (j.get("workflow_runs") or []):
        print("%-10s %-8s %s  %s" % (str(r.get("head_sha"))[:7], r.get("conclusion"),
                                     str(r.get("created_at"))[:19], r.get("name")))
    return 0


def cmd_logs(sha: str, want_job: str = "") -> int:
    # /actions/runs?head_sha= 只认完整 40 位 sha
    if len(sha) < 40:
        sha = _get("https://api.github.com/repos/" + REPO + "/commits/" + sha)["sha"]
    j = _get(API + "/actions/runs?head_sha=" + sha)
    runs = j.get("workflow_runs") or []
    if not runs:
        print("没有这个 sha 的 run:", sha)
        return 2
    rid = runs[0]["id"]
    print("# run", rid, "sha", sha)
    jobs = _get(API + "/actions/runs/" + str(rid) + "/jobs")
    for job in (jobs.get("jobs") or []):
        name = job.get("name") or ""
        if want_job and want_job not in name:
            continue
        print("\n## JOB", name, job.get("conclusion"))
        for st in (job.get("steps") or []):
            mark = "FAIL" if st.get("conclusion") == "failure" else "    "
            print("  %s %s" % (mark, st.get("name")))
        if job.get("conclusion") != "failure":
            continue
        try:
            txt = _get(API + "/actions/jobs/" + str(job["id"]) + "/logs", raw=True)
        except Exception as e:
            print("  （日志拉取失败：%s）" % type(e).__name__)
            continue
        lines = txt.splitlines()
        hits = [i for i, l in enumerate(lines) if "FATAL" in l or "Error:" in l or "FAIL" in l]
        for i in hits[-6:]:
            lo, hi = max(0, i - 2), min(len(lines), i + 3)
            print("  ---")
            for l in lines[lo:hi]:
                print("  " + l[:200])
    return 0


def cmd_file(rel: str, rev: str = "HEAD") -> int:
    url = API + "/contents/" + rel + "?ref=" + rev
    try:
        j = _get(url)
    except Exception as e:
        print("读不到:", type(e).__name__, str(e)[:120])
        return 2
    import base64
    data = base64.b64decode(j.get("content") or "")
    sys.stdout.buffer.write(data)
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 0
    if a[0] == "runs":
        return cmd_runs(int(a[1]) if len(a) > 1 else 5)
    if a[0] == "logs":
        return cmd_logs(a[1], a[2] if len(a) > 2 else "")
    if a[0] == "file":
        return cmd_file(a[1], a[2] if len(a) > 2 else "HEAD")
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())