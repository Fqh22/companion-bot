"""提交前扫一遍有没有把密钥/真地址带进仓库。

用法：
    python ops/scan-secrets.py            # 扫当前 git 跟踪的文件
    python ops/scan-secrets.py --staged   # 只扫暂存区（适合挂 pre-commit）
    python ops/scan-secrets.py 路径...

退出码 1 表示发现问题。输出会遮住密钥中间部分，避免二次泄露。
"""
from __future__ import annotations

import re
import subprocess
import sys

# 每条规则：名字、正则、说明
RULES = [
    ("api-key", re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
     "像 DeepSeek/OpenAI 的 API key"),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
     "私钥内容"),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
     "把 token 直接写在代码里"),
    ("public-ip", re.compile(r"\b(?:[1-9]\d{0,2}\.){3}[1-9]\d{0,2}\b"),
     "公网 IP（文档里请用 <SERVER_IP>）"),
    ("home-path", re.compile(r"[A-Za-z]:\\+Users\\+", re.I),
     "本地绝对路径（会带出用户名，文档里请用相对路径）"),
]

SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico", ".woff", ".woff2")


RESERVED = ("127.", "0.", "10.", "192.168.", "169.254.",
            "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31.")


def is_reserved(ip: str) -> bool:
    """内网/回环/链路本地地址不是泄露，测试用例里经常出现。"""
    return ip.startswith(RESERVED)


def mask(text: str) -> str:
    if len(text) <= 10:
        return text[:2] + "***"
    return text[:6] + "***" + text[-2:]


def files_from_git(staged: bool) -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged \
        else ["git", "ls-files"]
    # Windows 上 git 输出是 UTF-8，不显式指定会按 GBK 解码而炸掉
    out = subprocess.run(cmd, capture_output=True, text=True, check=False,
                         encoding="utf-8", errors="replace").stdout or ""
    return [x for x in out.splitlines() if x and not x.endswith(SKIP_SUFFIX)]


def check(paths: list[str]) -> int:
    bad = 0
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for no, line in enumerate(f, 1):
                    for name, rx, why in RULES:
                        m = rx.search(line)
                        if m:
                            # 占位符不算（文档里就是这么写的）
                            if name == "public-ip" and (
                                    "<SERVER_IP>" in line or is_reserved(m.group(0))):
                                continue
                            bad += 1
                            print(f"{path}:{no}  [{name}] {why}：{mask(m.group(0))}")
        except (OSError, UnicodeError):
            continue
    return bad


def main() -> int:
    args = sys.argv[1:]
    staged = "--staged" in args
    paths = [a for a in args if not a.startswith("--")] or files_from_git(staged)
    bad = check(paths)
    if bad:
        print(f"\n发现 {bad} 处可疑内容，先处理再提交。")
        return 1
    print(f"检查了 {len(paths)} 个文件，没发现问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
