"""NoneBot2 入口。"""
import logging

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.log import logger as _nb_logger

nonebot.init()


def _install_log_filter() -> None:
    """压掉 OneBot 的 notice（正在输入/戳一戳）日志，保留消息与错误。"""

    def _keep(record) -> bool:
        msg = record.get("message", "")
        return "notice.notify" not in msg and "input_status" not in msg

    try:
        for handler in _nb_logger._core.handlers.values():
            old = handler._filter

            def _make(old_filter):
                def _f(record):
                    if not _keep(record):
                        return False
                    return old_filter(record) if old_filter else True
                return _f

            handler._filter = _make(old)
    except Exception:
        pass


_install_log_filter()


def _bridge_stdlib_logging() -> None:
    """把 app_core 用的标准 logging 接到 nonebot 的 loguru 上。

    不接的话 `logging.getLogger("qqbot.chat")` 这类 logger 没有 handler，
    INFO 全被默默丢掉——召回明细、工具耗时、定时任务日志一条都看不见，
    线上排障只剩「用户说它不回」这种二手信息。
    """

    class _ToLoguru(logging.Handler):
        def emit(self, record):
            try:
                _nb_logger.opt(depth=6, exception=record.exc_info).log(
                    record.levelname, record.getMessage())
            except Exception:
                pass

    root = logging.getLogger("qqbot")
    root.setLevel(logging.INFO)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(_ToLoguru())


_bridge_stdlib_logging()

driver = nonebot.get_driver()
driver.register_adapter(Adapter)

nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
