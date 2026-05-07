"""structlog 중앙 설정 모듈.

애플리케이션 시작 시 configure_logging()을 호출하여 로깅을 초기화한다.
"""

import logging
import sys

import structlog


def configure_logging(
    log_level: str = "INFO", json_format: bool = True
) -> None:
    """structlog과 표준 logging을 설정한다.

    Args:
        log_level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR).
        json_format: True이면 JSON 포맷, False이면 콘솔 포맷.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_format:
        renderer: structlog.types.Processor = (
            structlog.processors.JSONRenderer()
        )
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(
        getattr(logging, log_level.upper(), logging.INFO)
    )


def bind_context(**kwargs: str) -> None:
    """로그 컨텍스트에 correlation 필드를 바인딩한다.

    Args:
        **kwargs: 바인딩할 키-값 쌍 (repo, issue_number, run_id 등).
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """로그 컨텍스트를 초기화한다."""
    structlog.contextvars.clear_contextvars()
