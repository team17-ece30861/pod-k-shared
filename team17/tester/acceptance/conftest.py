import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from acceptance.client import Contract, HTTPTransport, Service
from acceptance.reporting import operation_statuses, render_operation_table

RESULTS = {}
STARTED = None


def pytest_addoption(parser):
    parser.addoption("--base-url", default=os.environ.get("CRA_BASE_URL"))
    parser.addoption(
        "--allow-reset", action="store_true", help="Permit clearing this test deployment"
    )
    parser.addoption(
        "--tested-version", default=os.environ.get("CRA_TESTED_VERSION", "unspecified")
    )
    parser.addoption("--report", default=None, help="Write a JSON result report")
    parser.addoption("--operation-report", default=None, help="Write a Markdown operation table")


def pytest_sessionstart(session):
    global STARTED
    config = session.config
    STARTED = None
    RESULTS.clear()
    if config.option.collectonly:
        return
    if not config.getoption("--base-url"):
        raise pytest.UsageError("Provide --base-url or CRA_BASE_URL")
    if not os.environ.get("CRA_API_TOKEN"):
        raise pytest.UsageError("Set CRA_API_TOKEN in the environment")
    if not config.getoption("--allow-reset"):
        raise pytest.UsageError(
            "This suite resets all target data; "
            "pass --allow-reset for a coordinated test deployment"
        )
    try:
        HTTPTransport(config.getoption("--base-url"), os.environ["CRA_API_TOKEN"])
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    STARTED = time.time()


@pytest.fixture
def service(pytestconfig):
    return Service(HTTPTransport(pytestconfig.getoption("--base-url"), os.environ["CRA_API_TOKEN"]))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    service = item.funcargs.get("service")
    if service is None:
        return
    if report.when == "setup":
        service.operation_checks.clear()
    elif report.when == "call":
        report.user_properties.append(("operation_checks", service.operation_checks.copy()))


def pytest_runtest_logreport(report):
    entry = RESULTS.setdefault(
        report.nodeid,
        {"test": report.nodeid, "outcome": "not_run", "seconds": 0, "teardown_complete": False},
    )
    entry["seconds"] += report.duration
    if report.when == "teardown":
        entry["teardown_complete"] = True
    if report.when == "call":
        entry["operation_checks"] = dict(report.user_properties).get("operation_checks", [])
    if report.failed and "message" not in entry:
        crash = getattr(report.longrepr, "reprcrash", None)
        entry["message"] = crash.message if crash else str(report.longrepr)[-500:]
    if report.failed or entry["outcome"] != "failed" and (report.when == "call" or report.skipped):
        entry["outcome"] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    destination = session.config.getoption("--report")
    operation_destination = session.config.getoption("--operation-report")
    if STARTED is None:
        return
    results = list(RESULTS.values())
    operations = operation_statuses(Contract().document, results)
    report = {
        "base_url": session.config.getoption("--base-url"),
        "tested_version_label": session.config.getoption("--tested-version"),
        "started_at": datetime.fromtimestamp(STARTED, UTC).isoformat(),
        "finished_at": datetime.fromtimestamp(time.time(), UTC).isoformat(),
        "exit_code": int(exitstatus),
        "tests_run": len(results),
        "passed": sum(result["outcome"] == "passed" for result in results),
        "failed": sum(result["outcome"] == "failed" for result in results),
        "skipped": sum(result["outcome"] == "skipped" for result in results),
        "code_coverage": "not measured by external HTTP tests",
        "results": results,
        "operations": operations,
    }
    if destination is not None:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if operation_destination is not None:
        path = Path(operation_destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_operation_report(session.config, exitstatus, operations), encoding="utf-8")


def _operation_report(config, exitstatus, operations):
    version = config.getoption("--tested-version")
    completed = "finished" if exitstatus in (0, 1) else "incomplete"
    return (
        f"# Operation status\n\nVersion label: {version}\n\n"
        f"Run: {completed}; pytest exit code {int(exitstatus)}.\n\n"
        + render_operation_table(operations)
        + "\n"
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if STARTED is None:
        return
    operations = operation_statuses(Contract().document, RESULTS.values())
    terminalreporter.write_sep("=", "Operation status (observed evidence only)")
    for line in _operation_report(config, exitstatus, operations).splitlines():
        terminalreporter.write_line(line)
