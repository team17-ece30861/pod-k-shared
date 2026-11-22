"""Scoped metadata capture for checks made before a fixture can return its Service."""

from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT_CHECKS: ContextVar[list[dict] | None] = ContextVar("operation_checks", default=None)


@contextmanager
def capture_operation_checks():
    checks = []
    token = _CURRENT_CHECKS.set(checks)
    try:
        yield checks
    finally:
        _CURRENT_CHECKS.reset(token)


def observe_operation_check(check):
    checks = _CURRENT_CHECKS.get()
    if checks is not None:
        checks.append(check)
