# companion

> A self-hosted companion engine that **remembers you and has moods**.
> QQ is just one channel it can talk through — not the foundation.

Unofficial project, not affiliated with Tencent or QQ.

## What it is

Not a "QQ bot": a **channel-agnostic companion engine**.

```
your platform (QQ / terminal / Telegram / web / …)
        │  three things only: receive, send, typing
        ▼
   adapters/          platform fields → InboundMessage
        ▼
   Engine             one turn in, one turn out
        ▼
   app_core/          memory, mood, knowledge, profile, prompt, scheduling
        ▼
   TurnRunner         typing heartbeat, streaming, pacing, 45s deadline
        ▼
   your platform
```

- **Engine** (`nonebot/app_core/`): conversation, long-term memory, mood, knowledge,
  profile, contradiction checks, scheduling. Zero `import nonebot`; a lint enforces it.
- **Session loop** (`app_core/runner.py`): typing, streaming, pacing, timeout. Platform-free.
- **Adapt** (`nonebot/adapters/` + `plugins/`): translate a platform into `InboundMessage`
  and implement three send primitives.

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

That is all. Memory, mood, prompt, segmentation, pacing and the deadline are reused.
See [docs/06-移植指南.md](docs/06-移植指南.md) (Chinese).

## Quick start (no QQ needed)

Python **3.12** (CI only tests that version).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r nonebot/requirements.txt
export QQBOT_CODE_DIR=$PWD/nonebot
python ops/lint-plugins.py
python ops/test-nonebot-offline.py
```

To just talk to it, still without QQ: `python ops/drill-console.py` starts a terminal
channel on a temporary root — it never touches the production database.

## Provided adapters

| Adapter | File | Notes |
|---|---|---|
| console | `nonebot/adapters/console.py` | terminal; the reference implementation |
| QQ / OneBot v11 | `nonebot/adapters/qq.py` + `nonebot/plugins/` | via SnowLuma / NapCat etc. |

### Why QQ is only a demo

QQ has no official bot API for personal accounts. Access relies on OneBot v11
implementations that reverse-engineer the private protocol or hook the official client.
They can break at any time — protocol changes, risk control, or legal pressure
(one implementation received a cease-and-desist in Nov 2026). Using one carries a ban risk.

**So QQ lives at the outermost layer on purpose.** If it breaks, replace it; the engine stays.

## Capabilities

Streaming replies segmented by mood (18–32 chars), long-term memory with summarization and
vector recall, a three-value mood model, a self-written knowledge base with expiry,
a profile with confidence, contradiction detection, scheduling (diary/review/cleanup/weekly),
and rate-limited proactive messages.

## Data

Root defaults to `/opt`, override with `QQBOT_ROOT` (derived once in `app_core/paths.py`):
`data/` (chat.db, persona), `qqbot-data/` (memory db, keys.json, learn), `backup/`.
`config.yaml` and `keys.json` never enter git; template: `nonebot/config.example.yaml`.

## Testing

```bash
ops/verify.sh          # five suites: layers / offline / wiring / e2e / plugins
ops/test-runner.py     # channel-agnostic loop: streaming / typing / timeout
ops/drill-console.py   # offline drill: a real conversation with no QQ at all
```

## Docs

| Topic | File |
|---|---|
| How a message flows, where data lives | [docs/01-系统.md](docs/01-系统.md) |
| Deploy, backup, troubleshooting | [docs/02-运维.md](docs/02-运维.md) |
| Which test guards which wiring | [docs/04-联动矩阵.md](docs/04-联动矩阵.md) |
| Porting away from QQ | [docs/06-移植指南.md](docs/06-移植指南.md) |

## License

[MIT](LICENSE)
