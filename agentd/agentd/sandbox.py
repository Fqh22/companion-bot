"""沙箱：docker exec 封装（§3.1 生命周期 + §9.1 OOM 判定 + §10.2 溢出落盘）。

契约：命令只在容器里跑，容器只挂一个可写目录。
宿主侧绝不把模型给的字符串拼进 shell——argv 列表直接传给 docker。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .config import Config
from .verify import Observation


class Sandbox:
    def __init__(self, cfg: Config, work: Path) -> None:
        self.cfg = cfg
        self.work = work
        self.last_used = 0.0

    # ---------- 生命周期 ----------
    async def _docker(self, *args: str, timeout: float = 60) -> tuple[int, str, str]:
        return await self._run_argv(["docker", *args], timeout)

    async def _run_argv(self, argv: list[str], timeout: float = 60) -> tuple[int, str, str]:
        """跑一条宿主命令。测试里可整体替换这个方法。"""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout + 5)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = b"", b"host timeout"
        return (proc.returncode or 0,
                out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    async def state(self) -> str:
        code, out, _ = await self._docker(
            "inspect", "-f", "{{.State.Status}}", self.cfg.sandbox)
        return out.strip() if code == 0 else "missing"

    async def ensure(self) -> bool:
        st = await self.state()
        if st == "running":
            return True
        if st in ("exited", "created", "paused"):
            code, _, err = await self._docker("start", self.cfg.sandbox)
            return code == 0
        if st == "missing":
            return await self._create()
        return False

    async def _create(self) -> bool:
        argv = [
            "docker", "run", "-d", "--name", self.cfg.sandbox,
            "--network", self.cfg.net,
            "--read-only", "--tmpfs", "/tmp:size=64m",
            "--memory", self.cfg.sandbox_memory,
            "--pids-limit", "128", "--cpus", "0.8",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-e", f"HTTP_PROXY=http://{self.cfg.proxy_host}:{self.cfg.proxy_port}",
            "-e", f"HTTPS_PROXY=http://{self.cfg.proxy_host}:{self.cfg.proxy_port}",
            "-e", f"http_proxy=http://{self.cfg.proxy_host}:{self.cfg.proxy_port}",
            "-e", f"https_proxy=http://{self.cfg.proxy_host}:{self.cfg.proxy_port}",
            "-e", "no_proxy=localhost,127.0.0.1",
            "-v", f"{self.work}:/work:rw",
            "--entrypoint", "sleep",
            "agentd-sandbox:latest", "infinity",
        ]
        code, _, err = await self._run_argv(argv, timeout=120)
        return code == 0

    async def maybe_stop(self) -> None:
        """空闲超过阈值就停掉，把 512MB 还给系统。"""
        if not self.last_used:
            return
        if time.time() - self.last_used < self.cfg.idle_stop_seconds:
            return
        await self._docker("stop", "-t", "3", self.cfg.sandbox)

    async def oom(self) -> bool:
        code, out, _ = await self._docker(
            "inspect", "-f", "{{.State.OOMKilled}}", self.cfg.sandbox)
        return code == 0 and out.strip().lower() == "true"

    # ---------- 执行 ----------
    async def exec(self, cmd: str, task_id: int, timeout: int | None = None) -> tuple[Observation, str]:
        """返回 (观测量, 溢出日志路径)。"""
        if not await self.ensure():
            return Observation(exit_code=-1, stderr="沙箱不可用"), ""
        self.last_used = time.time()
        t = int(timeout or self.cfg.exec_timeout)
        inner = f"cd /work/{task_id} 2>/dev/null || cd /work; timeout -s KILL {t} sh -c {_q(cmd)}"
        code, out, err = await self._run_argv(
            ["docker", "exec", self.cfg.sandbox, "sh", "-c", inner], timeout=t + 10)

        logpath = ""
        if len(out) > self.cfg.out_inline:
            logpath = self._spill(task_id, out)
            out = _head_tail(out, self.cfg.out_inline, logpath)

        if code == 137 and await self.oom():
            err = (err + "\n[OOM] 容器因内存超限被内核杀掉").strip()
        return Observation(exit_code=code, stdout=out, stderr=err), logpath

    def _spill(self, task_id: int, text: str) -> str:
        d = self.work / str(task_id) / ".logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"step-{int(time.time())}.log"
        p.write_text(text, encoding="utf-8", errors="replace")
        return str(p)


def _q(s: str) -> str:
    """把命令包成 sh -c 的单个参数。"""
    return "'" + s.replace("'", "'\\''") + "'"


def _head_tail(text: str, limit: int, path: str) -> str:
    lines = text.splitlines()
    head, tail = lines[:20], lines[-20:]
    return ("\n".join(head) + f"\n…（共 {len(lines)} 行，完整内容见 {path}）…\n"
            + "\n".join(tail))[:limit]
