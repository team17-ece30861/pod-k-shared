from conftest import AUTH_ONLY, HEADERS


def set_clock(client, value: str):
    response = client.put("/test/clock", headers=AUTH_ONLY, json={"now": value})
    assert response.status_code == 200, response.text


def base_ticket(**overrides):
    body = {
        "source": "external_researcher",
        "title": "Authentication bypass",
        "description": "Simulated vulnerability.",
        "product": {
            "manufacturer": "Acme",
            "name": "Router OS",
            "affected_versions": ["4.1.0"],
        },
        **overrides,
    }
    return body


def full_ticket(**overrides):
    body = base_ticket(
        member_states=["DE"],
        vulnerability={
            "cve_id": "CVE-2026-1234",
            "description": "Authentication can be bypassed.",
        },
        exploit={"description": "Observed exploitation in production."},
        mitigations={
            "taken": "Blocked malicious sessions.",
            "for_users": "Upgrade immediately.",
        },
        sensitivity="AMBER",
        severity="high",
        impact="Remote access to administrative functions.",
        fix={
            "available_at": "2026-10-20T10:00:00Z",
            "description": "Patched authentication validation.",
            "fixed_versions": ["4.1.2"],
        },
    )
    body.update(overrides)
    return body


def create(client, body=None):
    response = client.post("/tickets", headers=HEADERS, json=body or base_ticket())
    assert response.status_code == 201, response.text
    return response.json()


def triage(client, ticket_id, aware_at="2026-10-19T10:00:00Z"):
    response = client.post(
        f"/tickets/{ticket_id}/triage",
        headers=HEADERS,
        json={
            "decision": "actively_exploited",
            "aware_at": aware_at,
            "reason": "Confirmed active exploitation.",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def audit(client, ticket_id):
    response = client.get(f"/tickets/{ticket_id}/audit", headers=AUTH_ONLY)
    assert response.status_code == 200, response.text
    return response.json()


def test_ac06_statuses_and_submitted_late_recalculate(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, full_ticket())
    triage(client, ticket["id"])

    before = client.get(f"/tickets/{ticket['id']}", headers=AUTH_ONLY).json()
    assert before["obligations"]["early_warning"]["status"] == "overdue"

    submitted = client.post(
        f"/tickets/{ticket['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={"submitted_at": "2026-10-20T11:00:00Z"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted_late"

    changed = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={
            "aware_at": "2026-10-19T12:00:00Z",
            "change_reason": "Corrected awareness evidence.",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["obligations"]["early_warning"]["status"] == "submitted"


def test_ac07_updates_recalculate_and_reject_invalid_times(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, base_ticket())
    triage(client, ticket["id"])

    changed = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={
            "aware_at": "2026-10-19T11:00:00Z",
            "change_reason": "Corrected evidence.",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["obligations"]["early_warning"]["due_at"] == "2026-10-20T11:00:00Z"

    future_fix = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={"fix": {"available_at": "2026-10-20T12:00:01Z"}},
    )
    assert future_fix.status_code == 422

    received = create(client, base_ticket(title="Still received"))
    bad_awareness = client.patch(
        f"/tickets/{received['id']}",
        headers=HEADERS,
        json={"aware_at": "2026-10-19T11:00:00Z", "change_reason": "No triage yet."},
    )
    assert bad_awareness.status_code == 409

    before_count = len(audit(client, ticket["id"]))
    noop = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={"title": changed.json()["title"]},
    )
    assert noop.status_code == 200
    assert len(audit(client, ticket["id"])) == before_count


def test_phase4_agreement_nonreportable_awareness_is_ignored(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client)
    response = client.post(
        f"/tickets/{ticket['id']}/triage",
        headers=HEADERS,
        json={
            "decision": "not_actively_exploited",
            "aware_at": "2026-10-19T10:00:00Z",
            "reason": "No evidence of exploitation.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "closed"
    assert response.json()["aware_at"] is None


def test_phase4_agreement_unknown_member_state_is_accepted(client):
    ticket = create(client, base_ticket(member_states=["unknown"]))
    assert ticket["member_states"] == ["unknown"]


def test_ac08_model_c_drafts_and_privacy(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(
        client,
        base_ticket(
            reporter={"name": "Reporter", "contact": "unique-private-contact"},
        ),
    )
    triage(client, ticket["id"])

    early = client.get(
        f"/tickets/{ticket['id']}/reports/early_warning",
        headers=AUTH_ONLY,
    )
    assert early.status_code == 200, early.text
    body = early.json()
    assert "member_states" in body["missing_fields"]
    assert "unique-private-contact" not in early.text

    updated = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={"member_states": ["DE"]},
    )
    assert updated.status_code == 200
    early = client.get(
        f"/tickets/{ticket['id']}/reports/early_warning",
        headers=AUTH_ONLY,
    ).json()
    assert early["missing_fields"] == []


def test_ac09_submission_order_missing_fields_and_locked_snapshot(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, full_ticket())
    triage(client, ticket["id"])

    notification_first = client.post(
        f"/tickets/{ticket['id']}/reports/notification/submission",
        headers=HEADERS,
        json={},
    )
    assert notification_first.status_code == 409

    early = client.post(
        f"/tickets/{ticket['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={"reference": "EW-1"},
    )
    assert early.status_code == 200, early.text
    locked_name = early.json()["content"]["product"]["name"]

    duplicate = client.post(
        f"/tickets/{ticket['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={},
    )
    assert duplicate.status_code == 409

    changed = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={"product": {"name": "Router OS Renamed"}},
    )
    assert changed.status_code == 200, changed.text

    reread = client.get(
        f"/tickets/{ticket['id']}/reports/early_warning",
        headers=AUTH_ONLY,
    )
    assert reread.status_code == 200
    assert reread.json()["content"]["product"]["name"] == locked_name

    incomplete = create(client, base_ticket(title="Incomplete"))
    triage(client, incomplete["id"])
    missing = client.post(
        f"/tickets/{incomplete['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={},
    )
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["field"] in {
        "product.affected_versions",
        "member_states",
    }


def test_ac10_complete_lifecycle_and_close(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, full_ticket())
    triage(client, ticket["id"])

    too_early = client.post(f"/tickets/{ticket['id']}/close", headers=HEADERS)
    assert too_early.status_code == 409

    for report_type in ("early_warning", "notification", "final_report"):
        response = client.post(
            f"/tickets/{ticket['id']}/reports/{report_type}/submission",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 200, response.text

    closed = client.post(f"/tickets/{ticket['id']}/close", headers=HEADERS)
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "closed"
    assert closed.json()["resolution"] == "resolved"

    locked = client.get(
        f"/tickets/{ticket['id']}/reports/final_report",
        headers=AUTH_ONLY,
    )
    assert locked.status_code == 200
    assert locked.json()["submitted_at"] is not None


def test_ac11_list_filters_and_tie_breaks(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    received = create(client, base_ticket(title="Received"))

    early = create(client, base_ticket(title="Earlier deadline"))
    triage(client, early["id"], "2026-10-19T10:00:00Z")

    later = create(client, base_ticket(title="Later deadline"))
    triage(client, later["id"], "2026-10-19T11:00:00Z")

    listing = client.get("/tickets", headers=AUTH_ONLY)
    assert listing.status_code == 200
    ids = [item["id"] for item in listing.json()]
    assert ids.index(early["id"]) < ids.index(later["id"]) < ids.index(received["id"])

    filtered = client.get(
        "/tickets",
        headers=AUTH_ONLY,
        params={"state": "reportable"},
    )
    assert {item["id"] for item in filtered.json()} == {early["id"], later["id"]}

    overdue = client.get(
        "/tickets",
        headers=AUTH_ONLY,
        params={"obligation_status": "overdue"},
    )
    assert {item["id"] for item in overdue.json()} == {early["id"], later["id"]}


def test_ac12_audit_is_append_only_and_failures_add_nothing(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, base_ticket())
    entries = audit(client, ticket["id"])
    assert [entry["seq"] for entry in entries] == [1]
    assert entries[0]["action"] == "created"

    triage(client, ticket["id"])
    entries = audit(client, ticket["id"])
    assert [entry["seq"] for entry in entries] == [1, 2]
    assert entries[-1]["actor"] == "pytest"

    failed = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={"aware_at": "2026-10-21T00:00:00Z", "change_reason": "future"},
    )
    assert failed.status_code == 422
    assert len(audit(client, ticket["id"])) == 2

    delete_attempt = client.delete(f"/tickets/{ticket['id']}/audit", headers=AUTH_ONLY)
    assert delete_attempt.status_code == 405


def test_ac12_reporter_only_update_is_audited_without_leaking_contact(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    secret_old = "old-private-contact"
    secret_new = "new-private-contact"
    ticket = create(
        client,
        base_ticket(reporter={"name": "Reporter", "contact": secret_old}),
    )

    changed = client.patch(
        f"/tickets/{ticket['id']}",
        headers=HEADERS,
        json={"reporter": {"name": "Reporter", "contact": secret_new}},
    )
    assert changed.status_code == 200, changed.text

    entries = audit(client, ticket["id"])
    assert len(entries) == 2
    assert entries[-1]["action"] == "updated"
    assert entries[-1]["changes"] == [
        {"field": "reporter", "old": "[redacted]", "new": "[redacted]"}
    ]
    assert secret_old not in str(entries)
    assert secret_new not in str(entries)


def test_ac13_many_malformed_requests_never_5xx_or_change_data(client):
    for i in range(500):
        response = client.post(
            "/tickets",
            headers=HEADERS,
            json={
                "source": "not-valid",
                "description": str(i),
                "product": {"manufacturer": "Acme", "name": "Router"},
            },
        )
        assert 400 <= response.status_code < 500
        assert response.headers["content-type"].startswith("application/problem+json")

    listing = client.get("/tickets", headers=AUTH_ONLY)
    assert listing.status_code == 200
    assert listing.json() == []


def test_ac16_reporter_confidentiality_across_reads_and_errors(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    secret = "unique-reporter-secret-42"
    ticket = create(
        client,
        full_ticket(reporter={"name": "Private Reporter", "contact": secret}),
    )
    triage(client, ticket["id"])

    detail = client.get(f"/tickets/{ticket['id']}", headers=AUTH_ONLY)
    assert detail.status_code == 200
    assert secret in detail.text

    responses = [
        client.get("/tickets", headers=AUTH_ONLY),
        client.get(f"/tickets/{ticket['id']}/audit", headers=AUTH_ONLY),
        client.get(
            f"/tickets/{ticket['id']}/reports/early_warning",
            headers=AUTH_ONLY,
        ),
        client.patch(
            f"/tickets/{ticket['id']}",
            headers=HEADERS,
            json={"fix": {"available_at": "2026-10-21T00:00:00Z"}},
        ),
    ]
    for response in responses:
        assert secret not in response.text



def test_team17_regression_actor_length_and_unknown_ticket_precedence(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, full_ticket())
    triage(client, ticket["id"])
    for report_type in ("early_warning", "notification", "final_report"):
        response = client.post(
            f"/tickets/{ticket['id']}/reports/{report_type}/submission",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 200, response.text

    long_actor = dict(AUTH_ONLY)
    long_actor["X-Actor"] = "x" * 201
    response = client.post(f"/tickets/{ticket['id']}/close", headers=long_actor)
    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "X-Actor"

    response = client.post(
        "/tickets/not-an-id/close",
        headers=AUTH_ONLY,
    )
    assert response.status_code == 404


def test_team17_regression_submission_validation_precedes_state_and_conflict(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    received = create(client, base_ticket())

    null_time = client.post(
        f"/tickets/{received['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={"submitted_at": None},
    )
    assert null_time.status_code == 400
    assert {error["field"] for error in null_time.json()["errors"]} == {"submitted_at"}

    null_reference = client.post(
        f"/tickets/{received['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={"reference": None},
    )
    assert null_reference.status_code == 400
    assert {error["field"] for error in null_reference.json()["errors"]} == {"reference"}

    null_body = client.request(
        "POST",
        f"/tickets/{received['id']}/reports/early_warning/submission",
        headers={**HEADERS, "Content-Type": "application/json"},
        content=b"null",
    )
    assert null_body.status_code == 400

    reportable = create(client, full_ticket(title="Duplicate validation"))
    triage(client, reportable["id"])
    first = client.post(
        f"/tickets/{reportable['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={},
    )
    assert first.status_code == 200
    duplicate_bad_body = client.post(
        f"/tickets/{reportable['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={"reference": None},
    )
    assert duplicate_bad_body.status_code == 400


def test_team17_regression_submission_aggregates_422_errors(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(
        client,
        full_ticket(member_states=[]),
    )
    triage(client, ticket["id"])

    response = client.post(
        f"/tickets/{ticket['id']}/reports/early_warning/submission",
        headers=HEADERS,
        json={"submitted_at": "2026-10-20T12:00:01Z"},
    )
    assert response.status_code == 422
    assert {error["field"] for error in response.json()["errors"]} == {
        "member_states",
        "submitted_at",
    }


def test_team17_regression_submission_target_precedes_body_and_actor(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    existing = create(client, base_ticket())

    response = client.request(
        "POST",
        f"/tickets/{existing['id']}/reports/user_advisory/submission",
        headers={**AUTH_ONLY, "Content-Type": "application/json"},
        content=b"{bad",
    )
    assert response.status_code == 404

    response = client.request(
        "POST",
        "/tickets/not-an-id/reports/early_warning/submission",
        headers={**AUTH_ONLY, "Content-Type": "application/json"},
        content=b"{bad",
    )
    assert response.status_code == 404


def test_team17_regression_auth_precedes_oversized_body(client):
    oversized = b"x" * 65537

    response = client.request(
        "POST",
        "/tickets/not-an-id/reports/unknown/submission",
        headers={"Content-Type": "application/json"},
        content=oversized,
    )
    assert response.status_code == 401

    response = client.request(
        "POST",
        "/tickets/not-an-id/reports/unknown/submission",
        headers={
            "Authorization": "Bearer definitely-wrong-token",
            "Content-Type": "application/json",
        },
        content=oversized,
    )
    assert response.status_code == 401


def test_team17_regression_user_advisory_includes_severity(client):
    set_clock(client, "2026-10-20T12:00:00Z")
    ticket = create(client, full_ticket())
    triage(client, ticket["id"])

    response = client.get(
        f"/tickets/{ticket['id']}/reports/user_advisory",
        headers=AUTH_ONLY,
    )
    assert response.status_code == 200
    content = response.json()["content"]
    assert content["severity"] == "high"
    assert content["mitigations"] == {"for_users": "Upgrade immediately."}


def test_team17_regression_method_not_allowed_uses_problem_json(client):
    ticket = create(client, base_ticket())
    response = client.delete(
        f"/tickets/{ticket['id']}/audit",
        headers=AUTH_ONLY,
    )
    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 405
