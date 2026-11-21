"""[domain] 有界去重：按到达顺序只保留最近 N 个 key，重复到达返回 False。

为什么不用 `set` + 超限 `clear()`：清空会连刚收到的 ID 一起抹掉，
平台重投旧事件时就能再次进入处理链路。这里逐个淘汰最旧的，容量边界前后
都只把「真正重复」判成重复。
"""
from __future__ import annotations

from collections import OrderedDict


class BoundedSeen:
    """容量有上限的 seen 集合；add() 返回 True 表示这是第一次见到。"""

    def __init__(self, maxsize: int = 500) -> None:
        self.maxsize = max(1, int(maxsize))
        self._items: "OrderedDict[str, None]" = OrderedDict()

    def add(self, key: str) -> bool:
        k = str(key or "")
        if not k:
            return True          # 没有 ID 的消息不参与去重，按新消息处理
        if k in self._items:
            self._items.move_to_end(k)
            return False
        self._items[k] = None
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)
        return True

    def __contains__(self, key: str) -> bool:
        return str(key or "") in self._items

    def __len__(self) -> int:
        return len(self._items)
