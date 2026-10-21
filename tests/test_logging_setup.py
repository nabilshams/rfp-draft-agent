"""Tests for console logging setup (so startup warnings are visible in container logs)."""

import logging

import pytest

from app.logging_setup import CONSOLE_HANDLER_NAME, configure_logging


@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_levels = {n: logging.getLogger(n).level for n in ("azure", "httpx", "openai")}
    root.handlers = [h for h in root.handlers if h.get_name() != CONSOLE_HANDLER_NAME]
    yield root
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def console_handlers(root):
    return [h for h in root.handlers if h.get_name() == CONSOLE_HANDLER_NAME]


def test_adds_one_console_handler_and_applies_the_level(clean_root_logger):
    configure_logging("DEBUG")
    configure_logging("INFO")  # called again, e.g. after a reload

    assert len(console_handlers(clean_root_logger)) == 1
    assert clean_root_logger.level == logging.INFO


def test_app_info_logs_reach_the_console_handler(clean_root_logger, capsys):
    configure_logging("INFO")

    logging.getLogger("app.auth.entra").warning("visible in container logs")

    assert "visible in container logs" in capsys.readouterr().err


def test_unknown_level_falls_back_to_info(clean_root_logger):
    configure_logging("nonsense")
    assert clean_root_logger.level == logging.INFO


def test_sdk_http_logging_is_quietened(clean_root_logger):
    configure_logging("DEBUG")
    assert logging.getLogger("azure").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
