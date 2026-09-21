"""集中式日志配置 + 请求级 traceId 注入。

此前项目完全依赖 uvicorn 的默认日志配置，而 uvicorn 只给 ``uvicorn`` /
``uvicorn.access`` 这两个 logger 装 handler 并设 ``propagate=False``，**从不配置
root logger**。后果是 ``app.*`` 的日志只能落到 Python 的 ``logging.lastResort``
兜底 handler 上:

* 它只输出 WARNING 及以上 —— 于是 ``TraceMiddleware`` 记录的每一个成功请求(INFO)、
  采集循环的启动信息、"关闭了 N 条孤儿告警"之类的 INFO 全部被静默丢弃;
* 它不带时间戳、级别和 logger 名 —— 侥幸输出的那几行也没法用来排障。

这里显式配置 root logger，并把当前请求的 traceId 注入每一行，使一次请求在服务层、
采集层产生的日志都能和访问日志串起来。traceId 本身早已由 ``TraceMiddleware``
生成并回写到 ``X-Trace-Id`` 响应头，这里只是把它带进日志。

后台线程池(告警通知 / 归因分析 / 采集)不在请求上下文里，traceId 显示为 ``-``，
这是符合预期的:它们本来就不属于任何一次请求。
"""

import logging
import logging.config
from contextvars import ContextVar, Token

_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRACE_ID: ContextVar[str] = ContextVar("dcn_trace_id", default="-")

_FORMAT = "%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 这些第三方库在 INFO 级极其吵:paramiko 每建一条 SSH 连接就打 2-3 行，而本平台
# 每 30-60s 会轮询一批设备，光它就能把应用日志整个淹掉。配好 root logger 之后它们
# 会开始传播上来，所以必须显式压到 WARNING。
# 例外:LOG_LEVEL=DEBUG 时全部放开——那正是需要看 SSH/HTTP 往返细节的时候。
_NOISY_LOGGERS = (
    "paramiko",
    "paramiko.transport",
    "urllib3",
    "requests",
    "httpx",
    "httpcore",
    "websockets",
    "asyncio",
    "watchfiles",
)


def set_trace_id(trace_id: str) -> Token:
    """绑定当前上下文的 traceId，返回用于复位的 token。"""
    return _TRACE_ID.set(trace_id or "-")


def reset_trace_id(token: Token) -> None:
    _TRACE_ID.reset(token)


def current_trace_id() -> str:
    return _TRACE_ID.get()


class TraceIdFilter(logging.Filter):
    """给每条日志记录补上 trace_id 字段。

    挂在 handler 上而不是 logger 上，这样经过该 handler 的**所有**记录(包括第三方
    库传播上来的)都一定带这个字段，不会因为缺字段而在格式化时抛错。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _TRACE_ID.get()
        return True


def _normalize_level(level: str | None) -> str:
    candidate = str(level or "INFO").strip().upper()
    if candidate not in _VALID_LEVELS:
        logging.getLogger(__name__).warning(
            "LOG_LEVEL=%r 不是合法级别(可选 %s)，回落到 INFO",
            level,
            "/".join(_VALID_LEVELS),
        )
        return "INFO"
    return candidate


def setup_logging(level: str | None = None) -> None:
    """配置 root logger。幂等，可重复调用(测试与 uvicorn 重载都会再跑一遍)。

    ``disable_existing_loggers=False`` 保证不会把 uvicorn 或第三方库已经建好的
    logger 关掉;uvicorn 自己的 logger 带 handler 且 ``propagate=False``，所以它的
    启动/访问日志格式不受影响，不会与这里重复输出。
    """
    resolved = _normalize_level(level)
    noisy_level = "DEBUG" if resolved == "DEBUG" else "WARNING"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"trace_id": {"()": "app.logging_config.TraceIdFilter"}},
            "formatters": {
                "standard": {"format": _FORMAT, "datefmt": _DATEFMT},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "filters": ["trace_id"],
                    "stream": "ext://sys.stderr",
                },
            },
            "loggers": {
                name: {"level": noisy_level, "propagate": True}
                for name in _NOISY_LOGGERS
            },
            "root": {"handlers": ["console"], "level": resolved},
        }
    )
