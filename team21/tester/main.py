from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlparse
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import REQUEST_TIMEOUT_SECONDS, TARGET_ACTOR, token_for_target
from .schemas import SessionRequest, SessionResponse, SuiteReport, TesterSetupRequest

app = FastAPI(
    title="Team 21 CRA Cross-Team Tester",
    version="0.4.2",
    description="Phase 4 tester for the Pod K CRA Article 14 reporting service.",
)


@dataclass
class TesterSession:
    session_id: str
    target_url: str
    finished: bool = False
    running: bool = False
    report: dict | None = None


_sessions: dict[str, TesterSession] = {}
_lock = Lock()
_http = httpx.Client(
    timeout=REQUEST_TIMEOUT_SECONDS,
    follow_redirects=True,
)


def _base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("target_url must be an HTTP(S) base URL")
    return value


def _headers(target_url: str, *, actor: bool = False) -> dict[str, str]:
    headers = {"accept": "application/json"}
    token = token_for_target(target_url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if actor:
        headers["X-Actor"] = TARGET_ACTOR
    return headers


def _request(method: str, target_url: str, path: str, *, json=None, actor=False) -> httpx.Response:
    return _http.request(
        method,
        f"{target_url}{path}",
        headers=_headers(target_url, actor=actor),
        json=json,
    )


def _message_from_response(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("message") or body.get("title") or body)
    return str(body)


def _result(test_id: str, passed: bool, message: str) -> dict:
    return {"test_id": test_id, "outcome": "pass" if passed else "fail", "message": message}


def _error_result(test_id: str, source: str, message: str) -> dict:
    return {"test_id": test_id, "outcome": "error", "source": source, "message": message}


def _reset_and_clock(target: str, when: str) -> None:
    r = _request("POST", target, "/test/reset", actor=True)
    if r.status_code != 204:
        raise RuntimeError(f"reset returned {r.status_code}: {_message_from_response(r)}")
    r = _request("PUT", target, "/test/clock", json={"now": when}, actor=True)
    if r.status_code != 200:
        raise RuntimeError(f"clock setup returned {r.status_code}: {_message_from_response(r)}")


def _ticket(title: str = "Auth bypass") -> dict:
    return {
        "source": "external_researcher",
        "title": title,
        "description": "Researcher reports login bypass using a crafted cookie.",
        "product": {
            "manufacturer": "Acme",
            "name": "Acme Router OS",
            "affected_versions": ["4.1.0", "4.1.1"],
        },
        "reporter": {"name": "J. Doe", "contact": "phase3-test@example.org"},
    }


def _run_suite(target: str) -> list[dict]:
    results: list[dict] = []

    # AC-01: two valid tickets at a fixed clock have the same receipt time and distinct IDs.
    try:
        _reset_and_clock(target, "2026-10-02T17:00:00Z")
        a = _request("POST", target, "/tickets", json=_ticket("One"), actor=True)
        b = _request("POST", target, "/tickets", json=_ticket("Two"), actor=True)
        ok = a.status_code == 201 and b.status_code == 201
        if ok:
            aj, bj = a.json(), b.json()
            ok = (
                aj.get("state") == bj.get("state") == "received"
                and aj.get("received_at") == bj.get("received_at") == "2026-10-02T17:00:00Z"
                and aj.get("id") != bj.get("id")
            )
        results.append(_result("AC-01", ok, "Valid ticket intake returns 201, fixed receipt time, and distinct IDs." if ok else "Ticket intake did not match AC-01."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-01", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-01", "tester", str(exc)))

    # AC-02: representative schema error.
    try:
        _reset_and_clock(target, "2026-10-02T17:00:00Z")
        bad = {
            "source": "email",
            "description": "x",
            "product": {"manufacturer": "Acme", "name": "Router"},
        }
        r = _request("POST", target, "/tickets", json=bad, actor=True)
        content_type = r.headers.get("content-type", "")
        body = r.json() if "json" in content_type else {}
        fields = {e.get("field") for e in body.get("errors", []) if isinstance(e, dict)} if isinstance(body, dict) else set()
        ok = r.status_code == 400 and {"source", "title"}.issubset(fields)
        results.append(_result("AC-02", ok, "Invalid source/missing title rejected with 400 and field errors." if ok else f"Expected 400 with source/title errors; got {r.status_code}."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-02", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-02", "tester", str(exc)))

    # AC-03: Model D E3 deadline example.
    try:
        _reset_and_clock(target, "2026-10-07T15:00:00Z")
        created = _request("POST", target, "/tickets", json=_ticket(), actor=True)
        if created.status_code != 201:
            raise RuntimeError(f"ticket create returned {created.status_code}")
        ticket_id = created.json()["id"]
        triage = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-07T14:30:00Z",
                "reason": "Customer logs show exploitation.",
            },
            actor=True,
        )
        ok = triage.status_code == 200
        if ok:
            data = triage.json()
            ok = (
                data.get("state") == "reportable"
                and data.get("obligations", {}).get("early_warning", {}).get("due_at") == "2026-10-08T14:30:00Z"
                and data.get("obligations", {}).get("notification", {}).get("due_at") == "2026-10-10T14:30:00Z"
            )
        results.append(_result("AC-03", ok, "Model D E3 24h/72h deadlines matched exactly." if ok else "Deadline calculation did not match Model D E3."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-03", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-03", "tester", str(exc)))

    # AC-04: future awareness rejected and ticket remains received.
    try:
        _reset_and_clock(target, "2026-10-07T15:00:00Z")
        created = _request("POST", target, "/tickets", json=_ticket(), actor=True)
        ticket_id = created.json()["id"]
        triage = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-07T15:00:01Z",
                "reason": "Future awareness test.",
            },
            actor=True,
        )
        reread = _request("GET", target, f"/tickets/{ticket_id}")
        ok = triage.status_code == 422 and reread.status_code == 200 and reread.json().get("state") == "received" and reread.json().get("aware_at") is None
        results.append(_result("AC-04", ok, "Future awareness was rejected with no ticket state change." if ok else "Future-awareness behavior did not match AC-04."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-04", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-04", "tester", str(exc)))

    # AC-05: not actively exploited closes ticket and re-triage conflicts.
    try:
        _reset_and_clock(target, "2026-10-07T15:00:00Z")
        created = _request("POST", target, "/tickets", json=_ticket(), actor=True)
        ticket_id = created.json()["id"]
        closed = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={"decision": "not_actively_exploited", "reason": "No evidence of exploitation."},
            actor=True,
        )
        again = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={"decision": "invalid", "reason": "Second triage."},
            actor=True,
        )
        ok = (
            closed.status_code == 200
            and closed.json().get("state") == "closed"
            and closed.json().get("resolution") == "not_actively_exploited"
            and again.status_code == 409
        )
        results.append(_result("AC-05", ok, "Non-reportable triage closed the ticket and prevented re-triage." if ok else "Non-reportable lifecycle did not match AC-05."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-05", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-05", "tester", str(exc)))

    # AC-14: timestamp offset normalization.
    try:
        reset = _request("POST", target, "/test/reset", actor=True)
        set_time = _request("PUT", target, "/test/clock", json={"now": "2026-10-05T11:00:00+02:00"}, actor=True)
        get_time = _request("GET", target, "/test/clock")
        ok = reset.status_code == 204 and set_time.status_code == 200 and get_time.status_code == 200 and get_time.json().get("now") == "2026-10-05T09:00:00Z"
        results.append(_result("AC-14", ok, "RFC 3339 offset normalized to UTC." if ok else "Timestamp normalization did not match AC-14."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-14", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-14", "tester", str(exc)))

    # AC-15: protected endpoint requires token; health does not.
    try:
        health = httpx.get(f"{target}/health", timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
        no_auth = httpx.post(f"{target}/tickets", headers={"X-Actor": TARGET_ACTOR}, json=_ticket(), timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
        ok = health.status_code == 200 and no_auth.status_code == 401
        results.append(_result("AC-15", ok, "Health is public and protected ticket creation rejects missing bearer token." if ok else f"Expected health 200 and unauthenticated ticket 401; got {health.status_code}/{no_auth.status_code}."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-15", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-15", "tester", str(exc)))

    # AC-17: reset + fixed clock.
    try:
        _reset_and_clock(target, "2026-10-11T16:00:00Z")
        created = _request("POST", target, "/tickets", json=_ticket(), actor=True)
        ticket_id = created.json()["id"]
        first = _request("GET", target, "/test/clock")
        second = _request("GET", target, "/test/clock")
        reset = _request("POST", target, "/test/reset", actor=True)
        missing = _request("GET", target, f"/tickets/{ticket_id}")
        ok = (
            created.status_code == 201
            and created.json().get("received_at") == "2026-10-11T16:00:00Z"
            and first.status_code == second.status_code == 200
            and first.json().get("now") == second.json().get("now") == "2026-10-11T16:00:00Z"
            and reset.status_code == 204
            and missing.status_code == 404
        )
        results.append(_result("AC-17", ok, "Reset removed ticket data and fixed clock remained deterministic." if ok else "Test reset/fixed clock behavior did not match AC-17."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-17", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-17", "tester", str(exc)))


    # AC-06: submitted-late status is derived from the recorded submission time.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        payload = _ticket("Phase 4 status")
        payload["product"]["affected_versions"] = ["4.1.0"]
        payload["member_states"] = ["DE"]
        created = _request("POST", target, "/tickets", json=payload, actor=True)
        ticket_id = created.json()["id"]
        triage = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-19T10:00:00Z",
                "reason": "Confirmed.",
            },
            actor=True,
        )
        submitted = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/reports/early_warning/submission",
            json={"submitted_at": "2026-10-20T11:00:00Z"},
            actor=True,
        )
        ok = (
            triage.status_code == 200
            and submitted.status_code == 200
            and submitted.json().get("status") == "submitted_late"
        )
        results.append(_result("AC-06", ok, "Submitted-late status matched the recalculated deadline." if ok else "Report status behavior did not match AC-06."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-06", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-06", "tester", str(exc)))

    # AC-07: awareness updates recalculate deadlines; future fix is rejected.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        created = _request("POST", target, "/tickets", json=_ticket("Phase 4 update"), actor=True)
        ticket_id = created.json()["id"]
        _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-19T10:00:00Z",
                "reason": "Confirmed.",
            },
            actor=True,
        )
        changed = _request(
            "PATCH",
            target,
            f"/tickets/{ticket_id}",
            json={
                "aware_at": "2026-10-19T11:00:00Z",
                "change_reason": "Corrected evidence.",
            },
            actor=True,
        )
        future = _request(
            "PATCH",
            target,
            f"/tickets/{ticket_id}",
            json={"fix": {"available_at": "2026-10-20T12:00:01Z"}},
            actor=True,
        )
        ok = (
            changed.status_code == 200
            and changed.json().get("obligations", {}).get("early_warning", {}).get("due_at")
            == "2026-10-20T11:00:00Z"
            and future.status_code == 422
        )
        results.append(_result("AC-07", ok, "Updates recalculated deadlines and rejected a future fix." if ok else "Update/recalculation behavior did not match AC-07."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-07", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-07", "tester", str(exc)))

    # AC-08: Model C draft reports missing required fields and excludes reporter data.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        payload = _ticket("Phase 4 draft")
        payload["product"]["affected_versions"] = ["4.1.0"]
        created = _request("POST", target, "/tickets", json=payload, actor=True)
        ticket_id = created.json()["id"]
        _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-19T10:00:00Z",
                "reason": "Confirmed.",
            },
            actor=True,
        )
        draft = _request("GET", target, f"/tickets/{ticket_id}/reports/early_warning")
        body = draft.json() if draft.status_code == 200 else {}
        ok = (
            draft.status_code == 200
            and "member_states" in body.get("missing_fields", [])
            and "reporter" not in body.get("content", {})
        )
        results.append(_result("AC-08", ok, "Draft missing-fields and reporter privacy matched Model C." if ok else "Draft behavior did not match AC-08."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-08", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-08", "tester", str(exc)))

    # AC-09: report order is enforced and a submitted report is locked.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        payload = _ticket("Phase 4 locking")
        payload["product"]["affected_versions"] = ["4.1.0"]
        payload["member_states"] = ["DE"]
        created = _request("POST", target, "/tickets", json=payload, actor=True)
        ticket_id = created.json()["id"]
        _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-19T10:00:00Z",
                "reason": "Confirmed.",
            },
            actor=True,
        )
        out_of_order = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/reports/notification/submission",
            json={},
            actor=True,
        )
        early = _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/reports/early_warning/submission",
            json={"reference": "EW-TEST"},
            actor=True,
        )
        locked_name = early.json().get("content", {}).get("product", {}).get("name") if early.status_code == 200 else None
        changed = _request(
            "PATCH",
            target,
            f"/tickets/{ticket_id}",
            json={"product": {"name": "Renamed Router"}},
            actor=True,
        )
        reread = _request("GET", target, f"/tickets/{ticket_id}/reports/early_warning")
        ok = (
            out_of_order.status_code == 409
            and early.status_code == 200
            and changed.status_code == 200
            and reread.status_code == 200
            and reread.json().get("content", {}).get("product", {}).get("name") == locked_name
        )
        results.append(_result("AC-09", ok, "Submission order and locked report snapshots behaved correctly." if ok else "Submission behavior did not match AC-09."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-09", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-09", "tester", str(exc)))

    # AC-10: scripted incident can submit all three reports and close.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        payload = _ticket("Phase 4 lifecycle")
        payload.update({
            "member_states": ["DE"],
            "vulnerability": {
                "cve_id": "CVE-2026-1234",
                "description": "Authentication bypass.",
            },
            "exploit": {"description": "Observed active exploitation."},
            "mitigations": {
                "taken": "Blocked malicious sessions.",
                "for_users": "Install the fixed version.",
            },
            "sensitivity": "AMBER",
            "severity": "high",
            "impact": "Administrative access.",
            "fix": {
                "available_at": "2026-10-20T10:00:00Z",
                "description": "Authentication validation corrected.",
                "fixed_versions": ["4.1.2"],
            },
        })
        payload["product"]["affected_versions"] = ["4.1.0"]
        created = _request("POST", target, "/tickets", json=payload, actor=True)
        ticket_id = created.json()["id"]
        _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={
                "decision": "actively_exploited",
                "aware_at": "2026-10-19T10:00:00Z",
                "reason": "Confirmed.",
            },
            actor=True,
        )
        statuses = []
        for report_type in ("early_warning", "notification", "final_report"):
            response = _request(
                "POST",
                target,
                f"/tickets/{ticket_id}/reports/{report_type}/submission",
                json={},
                actor=True,
            )
            statuses.append(response.status_code)
        closed = _request("POST", target, f"/tickets/{ticket_id}/close", actor=True)
        ok = statuses == [200, 200, 200] and closed.status_code == 200 and closed.json().get("resolution") == "resolved"
        results.append(_result("AC-10", ok, "Full submit-to-close incident completed successfully." if ok else "Full lifecycle did not match AC-10."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-10", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-10", "tester", str(exc)))

    # AC-11: filters work and nearest deadline is first.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        received = _request("POST", target, "/tickets", json=_ticket("Received"), actor=True).json()
        first = _request("POST", target, "/tickets", json=_ticket("First"), actor=True).json()
        _request(
            "POST",
            target,
            f"/tickets/{first['id']}/triage",
            json={"decision": "actively_exploited", "aware_at": "2026-10-19T10:00:00Z", "reason": "Confirmed."},
            actor=True,
        )
        second = _request("POST", target, "/tickets", json=_ticket("Second"), actor=True).json()
        _request(
            "POST",
            target,
            f"/tickets/{second['id']}/triage",
            json={"decision": "actively_exploited", "aware_at": "2026-10-19T11:00:00Z", "reason": "Confirmed."},
            actor=True,
        )
        listed = _request("GET", target, "/tickets")
        filtered = _request("GET", target, "/tickets?state=reportable")
        ids = [item.get("id") for item in listed.json()] if listed.status_code == 200 else []
        filtered_ids = {item.get("id") for item in filtered.json()} if filtered.status_code == 200 else set()
        ok = (
            listed.status_code == 200
            and filtered.status_code == 200
            and ids.index(first["id"]) < ids.index(second["id"]) < ids.index(received["id"])
            and filtered_ids == {first["id"], second["id"]}
        )
        results.append(_result("AC-11", ok, "Listing filters and deadline ordering behaved correctly." if ok else "List behavior did not match AC-11."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-11", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-11", "tester", str(exc)))

    # AC-12: successful changes append audit entries; failed changes do not.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        created = _request("POST", target, "/tickets", json=_ticket("Audit"), actor=True)
        ticket_id = created.json()["id"]
        _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={"decision": "actively_exploited", "aware_at": "2026-10-19T10:00:00Z", "reason": "Confirmed."},
            actor=True,
        )
        before = _request("GET", target, f"/tickets/{ticket_id}/audit")
        _request(
            "PATCH",
            target,
            f"/tickets/{ticket_id}",
            json={"aware_at": "2026-10-21T00:00:00Z", "change_reason": "Invalid future update."},
            actor=True,
        )
        after = _request("GET", target, f"/tickets/{ticket_id}/audit")
        entries = after.json() if after.status_code == 200 else []
        ok = (
            before.status_code == 200
            and after.status_code == 200
            and len(before.json()) == len(entries) == 2
            and [entry.get("seq") for entry in entries] == [1, 2]
        )
        results.append(_result("AC-12", ok, "Audit entries were append-only and failures added nothing." if ok else "Audit behavior did not match AC-12."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-12", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-12", "tester", str(exc)))

    # AC-13: 500 malformed requests must never produce a 5xx or create data.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        def malformed(i: int) -> bool:
            response = _request(
                "POST",
                target,
                "/tickets",
                json={
                    "source": "invalid",
                    "description": str(i),
                    "product": {"manufacturer": "Acme", "name": "Router"},
                },
                actor=True,
            )
            return 400 <= response.status_code < 500

        with ThreadPoolExecutor(max_workers=20) as pool:
            ok = all(pool.map(malformed, range(500)))
        listing = _request("GET", target, "/tickets")
        ok = ok and listing.status_code == 200 and listing.json() == []
        results.append(_result("AC-13", ok, "500 malformed requests produced no 5xx and no stored tickets." if ok else "Malformed-request behavior did not match AC-13."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-13", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-13", "tester", str(exc)))

    # AC-16: reporter contact appears only in the single-ticket response.
    try:
        _reset_and_clock(target, "2026-10-20T12:00:00Z")
        marker = "phase4-private-reporter-marker"
        payload = _ticket("Privacy")
        payload["reporter"] = {"name": "Private Reporter", "contact": marker}
        created = _request("POST", target, "/tickets", json=payload, actor=True)
        ticket_id = created.json()["id"]
        _request(
            "POST",
            target,
            f"/tickets/{ticket_id}/triage",
            json={"decision": "actively_exploited", "aware_at": "2026-10-19T10:00:00Z", "reason": "Confirmed."},
            actor=True,
        )
        detail = _request("GET", target, f"/tickets/{ticket_id}")
        listing = _request("GET", target, "/tickets")
        draft = _request("GET", target, f"/tickets/{ticket_id}/reports/early_warning")
        audit = _request("GET", target, f"/tickets/{ticket_id}/audit")
        ok = (
            detail.status_code == 200
            and marker in detail.text
            and marker not in listing.text
            and marker not in draft.text
            and marker not in audit.text
        )
        results.append(_result("AC-16", ok, "Reporter confidentiality held across list, draft, and audit reads." if ok else "Reporter confidentiality did not match AC-16."))
    except httpx.RequestError as exc:
        results.append(_error_result("AC-16", "communication", str(exc)))
    except Exception as exc:
        results.append(_error_result("AC-16", "tester", str(exc)))

    return results


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/test/setup", response_model=SessionResponse, status_code=201)
def setup(body: TesterSetupRequest):
    target = _base_url(str(body.target_url))
    session_id = f"run-{uuid.uuid4()}"

    try:
        response = _request(
            "POST",
            target,
            "/test/setup",
            json={"session_id": session_id, "setup": {}},
        )
    except httpx.RequestError as exc:
        return JSONResponse(status_code=500, content={"message": str(exc), "source": "communication"})

    if response.status_code != 201:
        source = "system"
        try:
            payload = response.json()
            message = str(payload.get("message") or payload.get("detail") or payload.get("title") or payload)
            source = str(payload.get("source") or source)
        except ValueError:
            message = response.text or f"System setup returned {response.status_code}"
        return JSONResponse(status_code=response.status_code, content={"message": message, "source": source})

    try:
        returned_id = response.json().get("session_id")
    except ValueError:
        returned_id = None
    if returned_id != session_id:
        return JSONResponse(status_code=500, content={"message": "Target system did not return the tester session ID.", "source": "system"})

    with _lock:
        _sessions[session_id] = TesterSession(session_id=session_id, target_url=target)
    return {"session_id": session_id}


@app.post("/test/run", response_model=SuiteReport)
def run(body: SessionRequest):
    with _lock:
        session = _sessions.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown tester session")
        if session.running or session.finished:
            raise HTTPException(status_code=409, detail="This tester session has already been run")
        session.running = True

    results = _run_suite(session.target_url)

    code_measure = {
        "covered": None,
        "total": None,
        "basis": "Target code coverage was unavailable because the target report could not be retrieved.",
    }
    try:
        coverage_response = _request("GET", session.target_url, f"/test/report?session_id={session.session_id}")
        if coverage_response.status_code == 200:
            code_measure = coverage_response.json().get("coverage", {}).get("code", code_measure)
    except httpx.RequestError:
        pass

    report = {
        "session_id": session.session_id,
        "target_url": session.target_url,
        "results": results,
        "coverage": {
            "code": code_measure,
            "component": {
                "covered": None,
                "total": None,
                "basis": "The shared Pod K specification does not define a component-coverage list for Phase 3.",
            },
            "system": {
                "covered": 17,
                "total": 19,
                "basis": "The Phase 4 tester checks AC-01 through AC-17 except AC-18 performance; AC-19 restart persistence is demonstrated separately.",
            },
        },
    }

    with _lock:
        session.report = report
        session.finished = True
        session.running = False
    return report


@app.get("/test/report", response_model=SuiteReport)
def report(session_id: str = Query(min_length=1)):
    with _lock:
        session = _sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown tester session")
        if not session.finished or session.report is None:
            raise HTTPException(status_code=409, detail="Tester run has not finished")
        return session.report
