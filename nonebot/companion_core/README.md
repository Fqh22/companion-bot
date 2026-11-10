# companion-core (mini)

M6-mini 的稳定入口。当前是 `app_core` 的门面，不是物理拆包。

~~~python
from companion_core import EngineConfig, create_engine

cfg = EngineConfig(root="/path/to/data-root")
engine = create_engine(cfg)
~~~

注意：`app_core.paths` 在 import 时读取 `QQBOT_ROOT`，所以先构造/apply
`EngineConfig`，再创建 Engine。

后续 M6-full 才把 `app_core` 物理搬进 `companion_core`、发布 PyPI。
