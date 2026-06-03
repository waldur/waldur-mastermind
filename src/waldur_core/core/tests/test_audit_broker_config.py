"""Tests for the ``audit_broker_config`` management command.

Each test calls the command via Django's call_command + override_settings
and inspects stdout, stderr, and the exit code (via SystemExit).
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings


def _run() -> tuple[int, str]:
    """Invoke the command, returning (exit_code, stdout)."""
    out = StringIO()
    with pytest.raises(SystemExit) as exc_info:
        call_command("audit_broker_config", stdout=out)
    return int(exc_info.value.code), out.getvalue()


@override_settings(
    CELERY_BROKER_HEARTBEAT=None,
    CELERY_BROKER_TRANSPORT_OPTIONS={
        "confirm_publish": True,
        "heartbeat": 30,
        "socket_settings": {257: 5, 258: 3},  # ints, like the real setting
    },
    CELERY_WORKER_MAX_TASKS_PER_CHILD=2000,
)
def test_all_ok_exits_zero():
    code, out = _run()
    assert code == 0
    assert "[OK] heartbeat" in out
    assert "[OK] socket_settings" in out
    assert "[OK] confirm_publish" in out
    assert "[OK] max_tasks_per_child" in out
    assert "ERROR=0" in out


@override_settings(
    CELERY_BROKER_HEARTBEAT=30,
    CELERY_BROKER_TRANSPORT_OPTIONS={"confirm_publish": True},
)
def test_heartbeat_only_at_top_level_warns():
    code, out = _run()
    assert code == 1
    assert "[WARN] heartbeat" in out
    assert "silently drops it" in out


@override_settings(
    CELERY_BROKER_HEARTBEAT=None,
    CELERY_BROKER_TRANSPORT_OPTIONS={"confirm_publish": True},
)
def test_no_heartbeat_warns():
    code, out = _run()
    assert code == 1
    assert "[WARN] heartbeat" in out
    assert "negotiate the broker's default" in out


@override_settings(
    CELERY_BROKER_TRANSPORT_OPTIONS={
        "confirm_publish": True,
        "heartbeat": 30,
        "socket_settings": {"TCP_KEEPIDLE": 10},  # string key — bug!
    },
)
def test_string_socket_settings_keys_are_error():
    code, out = _run()
    assert code == 2
    assert "[ERROR] socket_settings" in out
    assert "'TCP_KEEPIDLE'" in out


@override_settings(
    CELERY_BROKER_TRANSPORT_OPTIONS={
        "heartbeat": 30,
        "socket_settings": {257: 5},
    },
)
def test_missing_confirm_publish_warns():
    code, out = _run()
    assert code == 1
    assert "[WARN] confirm_publish" in out


@override_settings(
    CELERY_BROKER_TRANSPORT_OPTIONS={
        "confirm_publish": True,
        "heartbeat": 30,
        "socket_settings": {257: 5},
    },
    CELERY_WORKER_MAX_TASKS_PER_CHILD=100,
)
def test_low_max_tasks_warns():
    code, out = _run()
    assert code == 1
    assert "[WARN] max_tasks_per_child" in out
    assert "100" in out
