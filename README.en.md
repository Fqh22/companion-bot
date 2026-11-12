# Chat Kernel · companion

> A self-hosted chat kernel that **remembers you and has moods**.
> Platforms are adapters — QQ is one of them, not the foundation.

Unofficial project, not affiliated with Tencent or QQ.

## A short exchange

> Sample, redacted.

**Him**: I'm exhausted today.
**It**: Then stop pushing. Have you eaten?

**Him**: That 2K thing from last time — did you look it up?
**It**: Yes. DCI's 2K is 2048×1080; consumer displays call 2560×1440 2K too. The term itself is inconsistent.

## What it is

A **kernel plus adapters**, not a bot for one platform:

```
platform (QQ / terminal / Telegram / web / …)
   │  three primitives: receive, send, typing
   ▼
adapters/          platform fields → InboundMessage
   ▼
Engine             one turn in, one turn out
   ▼
app_core/          memory, mood, knowledge, profile, prompt, scheduling
   ▼
TurnRunner         typing heartbeat, streaming, pacing, 45s deadline
   ▼
platform
```

1. **The kernel knows nothing about platforms** — zero `import nonebot` in `app_core`, enforced by lint.
2. **A channel implements three primitives** — send text, send image, typing.
3. **Switching platforms never touches business code** — memory, mood, prompt, commands, scheduling are reused.

## Layers

Three layers inside the kernel, enforced by `ops/lint-layers.py`
(see [docs/06-扩展与移植.md](docs/06-扩展与移植.md)):

| Layer | Responsibility | Examples |
|---|---|---|
| **domain** | pure rules, no IO | `prompt` `pacing` `delivery` `intent` `mood_domain` |
| **orchestration** | one turn, commands, scheduling | `engine` `runner` `commands` `runtime` `chat` `scheduler` |
| **infra** | paths, config, files, clock | `paths` `config` `files` `kernel/*` |

## Adding a channel

Three primitives:

```python
class Channel:
    async def send_text(self, session: str, text: str) -> bool: ...
    async def send_image(self, session: str, path: str) -> bool: ...
    async def typing(self, session: str, on: bool) -> None: ...
```

```python
msg = InboundMessage(session=f"myplat:{user_id}", text=text, platform="myplat")
await TurnRunner(engine, MyChannel(), deadline=45).run(msg)
```

Commands come from a ready-made registry:

```python
registry = default_registry(engine.core)
reply = await registry.dispatch("/mood", CommandContext(session=my_session, is_owner=True))
```

That is all. See [docs/06-扩展与移植.md](docs/06-扩展与移植.md) (Chinese).

## Quick start (no platform needed)

Python **3.12** (CI only tests that version).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r nonebot/requirements.txt
export QQBOT_CODE_DIR=$PWD/nonebot
python ops/lint-plugins.py
python ops/test-nonebot-offline.py
```

To talk to it: `python ops/drill-console.py` — a terminal channel on a temporary root.

## Provided adapters

| Adapter | File | Notes |
|---|---|---|
| console | `nonebot/adapters/console.py` | terminal; the reference implementation |
| QQ / OneBot v11 | `nonebot/adapters/qq.py` + `nonebot/plugins/` | via SnowLuma / NapCat etc. |

### Why QQ is only a demo

QQ has no official bot API for personal accounts. Access relies on OneBot v11 implementations
that reverse-engineer the private protocol. They can break at any time — protocol changes,
risk control, or legal pressure (one implementation received a cease-and-desist in Nov 2026) —
and using them carries a ban risk.

**So QQ sits at the outermost layer on purpose.** If it breaks, replace it; the kernel stays.

## Capabilities

Streaming replies segmented by mood (18–32 chars), long-term memory with summarization and
vector recall, a three-value mood model, a self-written knowledge base with expiry, a profile
with confidence, contradiction detection, scheduling (diary/review/cleanup/weekly), and
rate-limited proactive messages.

## Data

Root defaults to `/opt`, override with `QQBOT_ROOT` (derived once in `app_core/paths.py`):
`data/` (chat.db, persona), `qqbot-data/` (memory db, keys.json, learn), `backup/`.

## Testing

```bash
ops/verify.sh          # five suites: layers / offline / wiring / e2e / plugins
ops/test-runner.py     # channel-agnostic loop: streaming / typing / timeout
ops/test-commands.py   # parsing / permissions / default registry
ops/drill-console.py   # offline drill: a real conversation with no QQ
```

## Docs

| Topic | File |
|---|---|
| Layers and where new modules go | [docs/06-扩展与移植.md](docs/06-扩展与移植.md) |
| Message flow and data layout | [docs/01-系统.md](docs/01-系统.md) |
| Deploy, backup, troubleshooting | [docs/02-运维.md](docs/02-运维.md) |
| Which test guards which wiring | [docs/04-联动矩阵.md](docs/04-联动矩阵.md) |
| Porting away from QQ | [docs/06-扩展与移植.md](docs/06-扩展与移植.md) |

## License

[MIT](LICENSE)
