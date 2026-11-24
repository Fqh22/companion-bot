# companion-core

应用入口与宿主无关。独立宿主从 `create_application()` 创建实例，再调用 `start()` 打开数据库，通过 `engine` 执行对话，最后调用 `stop()`。

~~~python
from pathlib import Path
from companion_core import EngineConfig, create_application

application = create_application(EngineConfig(Path("/path/to/data-root")))
await application.start()
try:
    # 宿主在这里接入控制台或自己的通道。
    engine = application.engine
finally:
    await application.stop()
~~~

NoneBot 用 `shared_application()` 将 QQ、飞书和调度接到同一实例。每个进程绑定一个数据根。运行多个独立机器人时，为各进程指定各自的数据根。配置须在加载内核数据路径前应用。现有 `create_engine()` 保留给已有宿主和测试。

普通回复和主动消息发送成功后统一调用 `record_delivery()`，在同一个事务中写历史及对应的跟进记录。通道存在可用的平台消息 ID 时可以据此识别引用。
