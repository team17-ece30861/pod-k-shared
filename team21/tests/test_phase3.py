from conftest import AUTH_ONLY, HEADERS


def set_clock(client, value: str):
    r = client.put("/test/clock", headers=HEADERS, json={"now": value})
    assert r.status_code == 200, r.text


def valid_ticket(title="Auth bypass"):
    return {
        "source": "external_researcher",
        "title": title,
        "description": "Researcher reports login bypass using a crafted cookie.",
        "product": {
            "manufacturer": "Acme",
            "name": "Acme Router OS",
            "affected_versions": ["4.1.0", "4.1.1"],
        },
        "reporter": {"name": "J. Doe", "contact": "jdoe@example.org"},
    }


def test_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ac01_two_valid_tickets_get_same_receipt_time_different_ids(client):
    set_clock(client, "2026-10-05T09:00:00Z")
    r1 = client.post("/tickets", headers=HEADERS, json=valid_ticket("One"))
    r2 = client.post("/tickets", headers=HEADERS, json=valid_ticket("Two"))
    assert r1.status_code == 201
    assert r2.status_code == 201
    a, b = r1.json(), r2.json()
    assert a["state"] == b["state"] == "received"
    assert a["received_at"] == b["received_at"] == "2026-10-05T09:00:00Z"
    assert a["id"] != b["id"]


def test_ac02_schema_violation_returns_400_and_creates_nothing(client):
    r = client.post(
        "/tickets",
        headers=HEADERS,
        json={
            "source": "email",
            "description": "x",
            "product": {"manufacturer": "Acme", "name": "Router"},
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == 400
    fields = {e["field"] for e in body["errors"]}
    assert "title" in fields
    assert "source" in fields


def test_ac03_deadline_calculation(client):
    set_clock(client, "2026-10-07T15:00:00Z")
    created = client.post("/tickets", headers=HEADERS, json=valid_ticket()).json()
    r = client.post(
        f"/tickets/{created['id']}/triage",
        headers=HEADERS,
        json={
            "decision": "actively_exploited",
            "aware_at": "2026-10-07T14:30:00Z",
            "reason": "Customer logs show exploitation.",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["state"] == "reportable"
    assert data["obligations"]["early_warning"]["due_at"] == "2026-10-08T14:30:00Z"
    assert data["obligations"]["notification"]["due_at"] == "2026-10-10T14:30:00Z"
    assert data["obligations"]["final_report"]["status"] == "awaiting_fix"
    assert data["next_due_at"] == "2026-10-08T14:30:00Z"


def test_ac04_future_awareness_rejected_and_ticket_unchanged(client):
    set_clock(client, "2026-10-07T15:00:00Z")
    created = client.post("/tickets", headers=HEADERS, json=valid_ticket()).json()
    r = client.post(
        f"/tickets/{created['id']}/triage",
        headers=HEADERS,
        json={
            "decision": "actively_exploited",
            "aware_at": "2026-10-07T16:00:00Z",
            "reason": "future time test",
        },
    )
    assert r.status_code == 422
    reread = client.get(f"/tickets/{created['id']}", headers=AUTH_ONLY).json()
    assert reread["state"] == "received"
    assert reread["aware_at"] is None


def test_ac05_not_actively_exploited_closes_ticket(client):
    created = client.post("/tickets", headers=HEADERS, json=valid_ticket()).json()
    r = client.post(
        f"/tickets/{created['id']}/triage",
        headers=HEADERS,
        json={"decision": "not_actively_exploited", "reason": "No evidence of exploitation."},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "closed"
    assert data["resolution"] == "not_actively_exploited"
    assert data["obligations"]["early_warning"]["status"] == "not_applicable"
    again = client.post(
        f"/tickets/{created['id']}/triage",
        headers=HEADERS,
        json={"decision": "invalid", "reason": "second triage"},
    )
    assert again.status_code == 409


def test_ac15_auth_required(client):
    r = client.post("/tickets", json=valid_ticket(), headers={"X-Actor": "pytest"})
    assert r.status_code == 401


def test_ac17_reset_and_fixed_clock(client):
    set_clock(client, "2026-10-11T12:00:00-04:00")
    created = client.post("/tickets", headers=HEADERS, json=valid_ticket()).json()
    assert created["received_at"] == "2026-10-11T16:00:00Z"
    r = client.post("/test/reset", headers=HEADERS)
    assert r.status_code == 204
    missing = client.get(f"/tickets/{created['id']}", headers=AUTH_ONLY)
    assert missing.status_code == 404


def test_ac14_timestamp_offset_normalized_and_naive_rejected(client):
    # Valid RFC 3339 offset is normalized to UTC.
    r = client.put(
        "/test/clock",
        headers=HEADERS,
        json={"now": "2026-10-11T12:00:00-04:00"},
    )
    assert r.status_code == 200
    assert r.json()["now"] == "2026-10-11T16:00:00Z"

    # A timestamp without an offset is rejected by the schema.
    r = client.put(
        "/test/clock",
        headers=HEADERS,
        json={"now": "2026-10-11T12:00:00"},
    )
    assert r.status_code == 400


def test_openapi_exposes_phase3_contract_paths(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for path in ["/health", "/tickets", "/tickets/{ticket_id}", "/tickets/{ticket_id}/triage", "/test/reset", "/test/clock"]:
        assert path in paths


def test_course_system_setup_and_report(client):
    setup = client.post(
        "/test/setup",
        headers=HEADERS,
        json={"session_id": "run-course-interface", "setup": {}},
    )
    assert setup.status_code == 201, setup.text
    assert setup.json() == {"session_id": "run-course-interface"}

    duplicate = client.post(
        "/test/setup",
        headers=HEADERS,
        json={"session_id": "run-course-interface", "setup": {}},
    )
    assert duplicate.status_code == 409

    report = client.get(
        "/test/report",
        headers=AUTH_ONLY,
        params={"session_id": "run-course-interface"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["session_id"] == "run-course-interface"
    assert body["coverage"]["code"]["covered"] is None
    assert body["coverage"]["code"]["total"] is None
    assert body["coverage"]["code"]["basis"]


def test_openapi_includes_course_cross_team_paths(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/test/setup" in paths
    assert "/test/report" in paths
