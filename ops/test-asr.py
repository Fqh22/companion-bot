"""语音转写：转码、时长、无 key 兜底。不联网。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

CODE = Path(os.environ.get("QQBOT_CODE_DIR", "/opt/nonebot")).resolve()
sys.path.insert(0, str(CODE))
os.environ.setdefault("QQBOT_ROOT", tempfile.mkdtemp(prefix="asr-test-"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app_core import asr  # noqa: E402

from harness import check, report  # noqa: E402


def main() -> int:
    import shutil

    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        print("SKIP（未执行）缺少 " + "、".join(missing))
        return 2

    with tempfile.TemporaryDirectory(prefix="asr-fixtures-") as temp:
        src = Path(temp) / "source.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(src)],
                       capture_output=True, check=True, timeout=60)

        wav = asr.to_wav(src)
        check("转码成功", wav is not None and wav.exists(), str(wav))
        if wav:
            try:
                d = asr.duration(wav)
                check("时长约 1 秒", 0.5 <= d <= 2.0, f"{d:.2f}")
                with patch.object(asr, "service_route", return_value=Mock(key=lambda: "")):
                    r = asyncio.run(asr.transcribe(wav))
                check("缺少凭证时给原因", r["text"] == "" and "凭证未配置" in r["error"],
                      r["error"][:30])
            finally:
                wav.unlink(missing_ok=True)

        bad = Path(temp) / "bad.txt"
        bad.write_text("not audio", encoding="utf-8")
        check("坏文件转码失败", asr.to_wav(bad) is None)

    return report()


if __name__ == "__main__":
    raise SystemExit(main())
