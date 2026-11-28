"""离线回归入口。默认按原顺序执行全套，可独立检查通道或发送节奏。"""
import argparse
import asyncio
import shutil
import sys


async def main(group: str) -> None:
    from offline_regression.fixtures import check_root_isolated

    check_root_isolated()
    try:
        if group == "adapters":
            from offline_regression import adapters

            await adapters.run()
        elif group == "pacing":
            from offline_regression import pacing

            await pacing.run()
        else:
            from offline_regression import conversation, foundation, learning, regressions

            core, fake_embed, learn_store_mod, mem, relational_mod = await foundation.run()
            if group == "foundation":
                return
            patch, wiki_mod = await conversation.run(core, fake_embed, learn_store_mod, mem, relational_mod)
            if group == "conversation":
                return
            _P, fresh_mood, prompt_fresh = await learning.run(core, mem, wiki_mod)
            if group == "learning":
                return
            await regressions.run(_P, core, fresh_mood, learn_store_mod, patch, prompt_fresh, wiki_mod)
    finally:
        # 收尾：取消遗留的后台任务（reply 会调度后台记忆总结），避免 loop 关闭后报错
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("all", "adapters", "pacing", "foundation", "conversation", "learning"), default="all")
    args = parser.parse_args()
    from offline_regression.fixtures import RESULTS, TMP

    try:
        asyncio.run(main(args.group))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(RESULTS)
    print(f"\n=== {passed}/{len(RESULTS)} 通过 ===")
    sys.exit(0 if passed == len(RESULTS) else 1)
