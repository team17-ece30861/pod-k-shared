from __future__ import annotations

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
    version="0.1.0",
    description="Phase 3 tester for the Pod K CRA vulnerability-ticketing service.",
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
    return httpx.request(
        method,
        f"{target_url}{path}",
        headers=_headers(target_url, actor=actor),
        json=json,
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
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
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
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
            actor=True,
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
                "covered": 8,
                "total": 19,
                "basis": "This Phase 3 tester contains one check each for AC-01 through AC-05, AC-14, AC-15, and AC-17 from the shared Pod K specification.",
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
