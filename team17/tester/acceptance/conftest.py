import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from acceptance.client import HTTPTransport, Service

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


def pytest_sessionstart(session):
    global STARTED
    config = session.config
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
    RESULTS.clear()


@pytest.fixture
def service(pytestconfig):
    return Service(HTTPTransport(pytestconfig.getoption("--base-url"), os.environ["CRA_API_TOKEN"]))


def pytest_runtest_logreport(report):
    entry = RESULTS.setdefault(
        report.nodeid, {"test": report.nodeid, "outcome": "not_run", "seconds": 0}
    )
    entry["seconds"] += report.duration
    if report.failed or entry["outcome"] != "failed" and (report.when == "call" or report.skipped):
        entry["outcome"] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    destination = session.config.getoption("--report")
    if destination is None or STARTED is None:
        return
    results = list(RESULTS.values())
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
    }
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
