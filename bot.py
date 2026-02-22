import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OnebotAdapter
import logging
import os

level_name = os.getenv("LOG_LEVEL", "INFO").upper()
level = getattr(logging, level_name, logging.INFO)
logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("kisaragirin.agent").setLevel(level)

# WebSocket keepalive ping/pong is normal; default to INFO to avoid noisy DEBUG spam.
websockets_level_name = os.getenv("WEBSOCKETS_LOG_LEVEL", "INFO").upper()
websockets_level = getattr(logging, websockets_level_name, logging.INFO)
logging.getLogger("websockets").setLevel(websockets_level)

# 初始化 NoneBot
nonebot.init()

# 注册适配器
driver = nonebot.get_driver()
driver.register_adapter(OnebotAdapter)

# 在这里加载插件
# nonebot.load_builtin_plugins("echo")  # 内置插件
# nonebot.load_plugin("thirdparty_plugin")  # 第三方插件
nonebot.load_plugins("zfnbot/plugins")  # 本地插件
if __name__ == "__main__":
    nonebot.run()
