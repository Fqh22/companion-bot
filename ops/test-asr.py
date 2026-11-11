"""语音转写：转码、时长、无 key 兜底。不联网。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", tempfile.mkdtemp(prefix="asr-test-"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import asr  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> None:
    import shutil

    if not shutil.which("ffmpeg"):
        # 没跑 ≠ 通过：本机（Windows）没有 ffmpeg 时返回 2，让 CI 不会把跳过当绿灯。
        print("SKIP（未执行）没有 ffmpeg，转码测试跳过")
        sys.exit(2)

    src = Path(tempfile.mkstemp(suffix=".wav")[1])
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(src)],
                   capture_output=True, timeout=60)

    wav = asr.to_wav(src)
    check("转码成功", wav is not None and wav.exists(), str(wav))
    if wav:
        d = asr.duration(wav)
        check("时长约 1 秒", 0.5 <= d <= 2.0, f"{d:.2f}")
        r = asyncio.run(asr.transcribe(wav))
        check("没 key 时给原因", r["text"] == "" and "key" in r["error"], r["error"][:30])
        wav.unlink(missing_ok=True)
    src.unlink(missing_ok=True)

    bad = Path(tempfile.mkstemp(suffix=".txt")[1])
    bad.write_text("not audio", encoding="utf-8")
    check("坏文件转码失败", asr.to_wav(bad) is None)
    bad.unlink(missing_ok=True)

    passed = sum(1 for x in RESULTS if x)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
