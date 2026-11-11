from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.settings import settings


_CONFIGURED = False
_RESERVED_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__.keys())
# logging.Formatter.format 会在运行时向 record 注入 asctime/message 两个派生字段。
# 若不排除，会被当作 extra 再次拼接，造成“一行日志看起来像两份内容”的重复现象。
_RESERVED_CONTEXT_KEYS = _RESERVED_RECORD_KEYS | {"asctime", "message"}


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base_message = super().format(record)
        context = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_CONTEXT_KEYS and not key.startswith("_")
        }
        if context:
            context_text = " ".join(f"{key}={value}" for key, value in sorted(context.items()))
            base_message = f"{base_message} | {context_text}"
        # 长轮询地址带渠道凭据，应用日志和异常堆栈均不能保存原始 Token。
        return re.sub(r"(/api/wechat-webridge/\d+/)[A-Za-z0-9_-]{43}(?=[/\s?\"']|$)", r"\1[已隐藏]", base_message)


class ColorConsoleFormatter(ContextFormatter):
    RESET = "\033[0m"
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35;1m",
    }

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        color = self.COLORS.get(record.levelname.upper(), "")
        if not color:
            return rendered
        return f"{color}{rendered}{self.RESET}"


class SafeExtraLogger(logging.Logger):
    def makeRecord(self, *args, **kwargs):  # type: ignore[override]
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                key if key not in _RESERVED_RECORD_KEYS else f"ctx_{key}": value
                for key, value in extra.items()
            }
        return super().makeRecord(*args, **kwargs)


def _prune_excess_backups(log_dir: Path, base_name: str, keep: int) -> None:
    """删除超出保留数量的历史备份(app.log.5 及更大序号)。

    RotatingFileHandler 只会在轮转时覆盖到 backupCount,调小保留数量后,
    更大序号的旧文件不会被自动清理,这里在启动时补一次,保证磁盘占用上限。
    """

    for path in log_dir.glob(f"{base_name}.*"):
        suffix = path.name[len(base_name) + 1 :]
        if suffix.isdigit() and int(suffix) > keep:
            try:
                path.unlink()
            except OSError:
                pass


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.setLoggerClass(SafeExtraLogger)

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    for base_name in ("app.log", "error.log"):
        _prune_excess_backups(log_dir, base_name, int(settings.log_file_backup_count))

    level_name = str(settings.log_level or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s"
    log_datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = ContextFormatter(
        fmt=log_format,
        datefmt=log_datefmt,
    )
    console_formatter = ColorConsoleFormatter(
        fmt=log_format,
        datefmt=log_datefmt,
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    app_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=int(settings.log_file_max_bytes),
        backupCount=int(settings.log_file_backup_count),
        encoding="utf-8",
    )
    app_handler.setLevel(level)
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    error_handler = RotatingFileHandler(
        log_dir / "error.log",
        maxBytes=int(settings.log_file_max_bytes),
        backupCount=int(settings.log_file_backup_count),
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    noisy_logger_levels = {
        "uvicorn": level,
        "uvicorn.error": level,
        "uvicorn.access": logging.WARNING,
        "sqlalchemy.engine": logging.WARNING,
        "asyncio": logging.WARNING,
        "httpcore": logging.WARNING,
        "httpx": logging.WARNING,
        "watchfiles": logging.WARNING,
    }
    for logger_name, logger_level in noisy_logger_levels.items():
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logger_level)
        logger.propagate = True

    logging.captureWarnings(True)
    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "logging configured",
        extra={
            "log_dir": str(log_dir),
            "log_level": level_name,
        },
    )


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
