import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OnebotAdapter
import logging
import sys
from nonebot.log import logger, default_format


class InterceptHandler(logging.Handler):
    """把标准 logging 转发到 loguru，并保留原始 record.name 避免显示 __main__。"""

    def emit(self, record: logging.LogRecord):
        # 拦截 uvicorn 和 fastapi 的重复日志，因为它们由 driver 自身配置了专门的 Handler 转发到 Loguru
        if record.name.startswith("uvicorn") or record.name.startswith("fastapi"):
            return
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # patch name 保证 loguru 的 record["name"] 与标准 logging 的 record.name 一致
        logger.patch(lambda r: r.update(name=record.name)).opt(
            exception=record.exc_info
        ).log(level, record.getMessage())


# 1. 重定向标准 logging 到 NoneBot 的 Loguru 实现彩色和统一格式
logging.basicConfig(handlers=[InterceptHandler()], level=logging.DEBUG, force=True)

# 2. 覆盖默认的 Loguru 处理器日志过滤规则（忽略环境变量，只给指定项开启 DEBUG）
logger.remove()

def custom_log_filter(record):
    name = record.get("name", "")
    # 对指定的插件或模块开启 DEBUG，其余一律保持 WARNING
    if name.startswith("kisaragirin") or name.startswith("zfnbot"):
        levelno = logger.level("DEBUG").no
    else:
        levelno = logger.level("WARNING").no
    return record["level"].no >= levelno

logger.add(
    sys.stdout,
    filter=custom_log_filter,
    format=default_format,
    diagnose=False,
    colorize=True,
)

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
