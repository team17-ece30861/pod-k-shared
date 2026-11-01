"""Operation-level evidence from executed checks, not implementation declarations."""

import re
from collections.abc import Iterable

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
SPEC_REQUIREMENTS = {
    ("GET", "/test/clock"): ["REQ-16"],
    ("DELETE", "/test/clock"): ["REQ-16"],
}


def _conformance(expected: list[int], checks: list[tuple[dict, str]]) -> dict:
    passed = set()
    failed = set()
    inconclusive = set()
    for check, outcome in checks:
        status = check["expected_status"]
        if not check["conforms"]:
            failed.add(status)
        elif outcome == "passed":
            passed.add(status)
        else:
            inconclusive.add(status)
    untested = set(expected) - passed - failed - inconclusive
    if failed:
        result = "failed"
    elif inconclusive:
        result = "inconclusive"
    elif not expected:
        result = "not_defined"
    elif not passed:
        result = "not_tested"
    elif untested:
        result = "partial"
    else:
        result = "passed"
    return {
        "status": result,
        "expected_statuses": expected,
        "passed_statuses": sorted(passed),
        "failed_statuses": sorted(failed),
        "inconclusive_statuses": sorted(inconclusive),
        "untested_statuses": sorted(untested),
        "checks": len(checks),
    }


def operation_statuses(document: dict, results: Iterable[dict]) -> list[dict]:
    """Summarize only Service.expect checks; auth failures cannot prove a feature.

    Passing observations also require a passing enclosing test and completed
    teardown. A valid response from an unfinished, failed or skipped test is
    inconclusive, not conformance proof.
    Requirement IDs come from operation descriptions, except clock read/release
    use REQ-16 from docs/spec.md where their descriptions omit the requirement.
    """
    observations: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    for result in results:
        outcome = result["outcome"]
        if outcome == "passed" and not result.get("teardown_complete", False):
            outcome = "not_run"
        for check in result.get("operation_checks", []):
            key = (check["method"], check["path"])
            observations.setdefault(key, []).append((check, outcome))

    rows = []
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            key = (method.upper(), path)
            checks = observations.get(key, [])
            statuses = sorted(int(status) for status in operation["responses"] if status.isdigit())
            success = _conformance(
                [status for status in statuses if 200 <= status < 300],
                [check for check in checks if 200 <= check[0]["expected_status"] < 300],
            )
            error = _conformance(
                [status for status in statuses if 400 <= status < 600],
                [check for check in checks if 400 <= check[0]["expected_status"] < 600],
            )
            if success["passed_statuses"]:
                implementation = "success_observed"
            elif success["inconclusive_statuses"]:
                implementation = "inconclusive"
            else:
                implementation = "not_demonstrated"
            description = operation.get("description", "")
            requirements = sorted(set(re.findall(r"\bREQ-[0-9]{2}\b", description)))
            rows.append(
                {
                    "method": key[0],
                    "path": path,
                    "operation_id": operation.get("operationId"),
                    "requirements": requirements or SPEC_REQUIREMENTS.get(key, []),
                    "implementation": implementation,
                    "success": success,
                    "error": error,
                }
            )
    return rows


def _cell(conformance: dict) -> str:
    details = []
    for label in ("passed", "failed", "inconclusive", "untested"):
        statuses = conformance[f"{label}_statuses"]
        if statuses:
            details.append(f"{label}: " + ", ".join(map(str, statuses)))
    status = conformance["status"].replace("_", " ")
    return status + (" (" + "; ".join(details) + ")" if details else "")


def render_operation_table(rows: list[dict]) -> str:
    """Render a report-ready table with explicit evidence limitations."""
    lines = [
        "| Method and path | Requirements | Implemented (evidence) | "
        "Success responses | Error responses |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        requirements = ", ".join(row["requirements"]) or "none listed"
        implementation = row["implementation"].replace("_", " ")
        lines.append(
            f"| {row['method']} {row['path']} | {requirements} | {implementation} | "
            f"{_cell(row['success'])} | {_cell(row['error'])} |"
        )
    lines.extend(
        [
            "",
            "Success observed means a contract-checked expected 2xx in a passing test, "
            "not full feature coverage.",
            "Passed/partial describe observed checks and explicit status-code coverage, "
            "not every behavior.",
            "Untested statuses are not passes. Unfinished/failed/skipped tests "
            "never count as passing evidence.",
            "Fixture setup/reset calls and raw transport calls are excluded. "
            "No code coverage is inferred.",
        ]
    )
    return "\n".join(lines)
