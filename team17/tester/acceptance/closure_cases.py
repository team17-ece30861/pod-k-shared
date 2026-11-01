"""Portable closure and append-only full-lifecycle checks (AC-10/12)."""

import json
from copy import deepcopy

import pytest

from acceptance.draft_cases import FIELDS, FIXED, REPORTER, create_reportable, read_draft
from acceptance.submission_cases import REPORT_TYPES, audit_log, submit_report

UNKNOWN = "00000000-0000-0000-0000-000000000000"


def close_ticket(service, ticket_id, status=200, **kwargs):
    return service.expect(
        "POST", f"/tickets/{ticket_id}/close", status, template="/tickets/{id}/close", **kwargs
    )


def ready_to_close(service):
    ticket_id = create_reportable(service, **deepcopy(FIELDS))
    for report_type in REPORT_TYPES:
        submit_report(service, ticket_id, report_type)
    return ticket_id


def closure_state(service, ticket_id):
    ticket = service.get(ticket_id)
    reports = {}
    if ticket["state"] == "reportable" or ticket["resolution"] == "resolved":
        reports = {
            report_type: read_draft(service, ticket_id, report_type)
            for report_type in (*REPORT_TYPES, "user_advisory")
        }
    return {"ticket": ticket, "reports": reports, "audit": audit_log(service, ticket_id)}


class ClosureChecks:
    @pytest.fixture(autouse=True)
    def fresh_closure_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    @pytest.mark.parametrize("submitted_count", [0, 1, 2])
    def test_ac10_outstanding_reports_reject_closure_without_changes(
        self, service, submitted_count
    ):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        for report_type in REPORT_TYPES[:submitted_count]:
            submit_report(service, ticket_id, report_type)
        before = closure_state(service, ticket_id)
        close_ticket(service, ticket_id, 409)
        assert closure_state(service, ticket_id) == before

    def test_ac10_all_three_reports_allow_one_atomic_close_and_remain_readable(self, service):
        ticket_id = ready_to_close(service)
        before = closure_state(service, ticket_id)
        closed = close_ticket(service, ticket_id, actor="closer")
        assert closed["state"] == "closed"
        assert closed["resolution"] == "resolved"
        assert "reporter" not in closed
        after = closure_state(service, ticket_id)
        assert after["ticket"]["state"] == "closed"
        assert after["ticket"]["resolution"] == "resolved"
        assert after["reports"] == before["reports"]
        assert after["audit"][:-1] == before["audit"]
        entry = after["audit"][-1]
        assert entry["seq"] == 6
        assert entry["action"] == "closed"
        assert entry["at"] == FIXED
        assert entry["actor"] == "closer"
        assert entry["reason"] is None
        assert entry["changes"]
        assert REPORTER not in json.dumps({"closed": closed, "audit": after["audit"]})
        listed = next(ticket for ticket in service.list() if ticket["id"] == ticket_id)
        assert listed == closed
        assert closure_state(service, ticket_id) == after

    @pytest.mark.parametrize("state", ["received", "invalid", "not_actively_exploited"])
    def test_ac10_never_reportable_tickets_cannot_be_resolved(self, service, state):
        ticket_id = service.create()["id"]
        if state != "received":
            service.triage(ticket_id, {"decision": state, "reason": "Reviewed"})
        before = closure_state(service, ticket_id)
        close_ticket(service, ticket_id, 409)
        assert closure_state(service, ticket_id) == before

    @pytest.mark.parametrize(
        "method,suffix,body",
        [
            ("PATCH", "", {"title": "Cannot change"}),
            (
                "PATCH",
                "",
                {"aware_at": FIXED, "change_reason": "Cannot change awareness"},
            ),
            (
                "POST",
                "/triage",
                {"decision": "invalid", "reason": "Cannot retriage"},
            ),
            ("POST", "/reports/early_warning/submission", None),
            ("POST", "/reports/notification/submission", None),
            ("POST", "/reports/final_report/submission", None),
            ("POST", "/close", None),
        ],
        ids=["details", "awareness", "triage", "early", "notification", "final", "close"],
    )
    def test_ac10_resolved_ticket_rejects_every_change(self, service, method, suffix, body):
        ticket_id = ready_to_close(service)
        close_ticket(service, ticket_id)
        before = closure_state(service, ticket_id)
        template = "/tickets/{id}" + suffix
        if "/reports/" in suffix:
            template = "/tickets/{id}/reports/{type}/submission"
        service.expect(method, f"/tickets/{ticket_id}{suffix}", 409, template=template, body=body)
        assert closure_state(service, ticket_id) == before

    @pytest.mark.parametrize(
        "actor", [None, "", "   ", "x" * 201], ids=["absent", "empty", "blank", "long"]
    )
    def test_close_requires_valid_actor_without_changes(self, service, actor):
        ticket_id = ready_to_close(service)
        before = closure_state(service, ticket_id)
        problem = close_ticket(service, ticket_id, 400, actor=actor)
        assert any(error["field"] == "X-Actor" for error in problem["errors"])
        assert closure_state(service, ticket_id) == before

    def test_actor_error_precedes_received_state_conflict(self, service):
        ticket_id = service.create()["id"]
        before = closure_state(service, ticket_id)
        close_ticket(service, ticket_id, 400, actor=None)
        assert closure_state(service, ticket_id) == before

    @pytest.mark.parametrize("ticket_id", [UNKNOWN, "not-an-id"])
    def test_unknown_ticket_precedes_missing_actor(self, service, ticket_id):
        existing = ready_to_close(service)
        before = closure_state(service, existing)
        close_ticket(service, ticket_id, 404, actor=None)
        assert closure_state(service, existing) == before

    @pytest.mark.parametrize("unknown", [False, True])
    @pytest.mark.parametrize("authenticated,token", [(False, None), (True, "wrong-token")])
    def test_authentication_precedes_missing_resource_and_actor(
        self, service, unknown, authenticated, token
    ):
        ticket_id = ready_to_close(service)
        before = closure_state(service, ticket_id)
        close_ticket(
            service,
            UNKNOWN if unknown else ticket_id,
            401,
            actor=None,
            authenticated=authenticated,
            token=token,
        )
        assert closure_state(service, ticket_id) == before

    @pytest.mark.parametrize("unknown", [False, True])
    def test_oversized_close_precedes_resource_and_actor_errors(self, service, unknown):
        ticket_id = ready_to_close(service)
        before = closure_state(service, ticket_id)
        target = UNKNOWN if unknown else ticket_id
        response = service.transport.send(
            "POST", f"/tickets/{target}/close", raw=b"x" * 65537, actor=None
        )
        assert response.status == 413
        assert response.content_type.split(";", 1)[0] == "application/problem+json"
        assert json.loads(response.body)["status"] == 413
        assert closure_state(service, ticket_id) == before

    @pytest.mark.parametrize("authenticated,token", [(False, None), (True, "wrong-token")])
    def test_authentication_precedes_oversized_close(self, service, authenticated, token):
        ticket_id = ready_to_close(service)
        before = closure_state(service, ticket_id)
        close_ticket(
            service,
            UNKNOWN,
            401,
            raw=b"x" * 65537,
            actor=None,
            authenticated=authenticated,
            token=token,
        )
        assert closure_state(service, ticket_id) == before

    @pytest.mark.parametrize("now", ["2026-10-01T09:00:00Z", "2026-11-01T09:00:00Z"])
    def test_close_depends_on_recorded_reports_not_current_clock(self, service, now):
        ticket_id = ready_to_close(service)
        service.fix_clock(now)
        before = closure_state(service, ticket_id)
        close_ticket(service, ticket_id)
        after = closure_state(service, ticket_id)
        assert after["reports"] == before["reports"]
        assert after["audit"][-1]["at"] == now

    def test_close_does_not_revalidate_locked_report_content(self, service):
        ticket_id = ready_to_close(service)
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"impact": None, "product": {"affected_versions": []}},
        )
        before = closure_state(service, ticket_id)
        close_ticket(service, ticket_id)
        assert closure_state(service, ticket_id)["reports"] == before["reports"]

    def test_close_does_not_change_a_different_ticket(self, service):
        ticket_id = ready_to_close(service)
        other_id = create_reportable(service, **deepcopy(FIELDS))
        before = closure_state(service, other_id)
        close_ticket(service, ticket_id)
        assert closure_state(service, other_id) == before

    def test_ac12_scripted_lifecycle_has_exact_immutable_audit_sequence(self, service):
        ticket_id = service.create(**deepcopy(FIELDS))["id"]
        close_ticket(service, ticket_id, 409)
        service.triage(
            ticket_id,
            {"decision": "actively_exploited", "aware_at": FIXED, "reason": "Confirmed"},
            actor="triager",
        )
        submit_report(service, ticket_id, "notification", 409)
        service.fix_clock("2026-10-05T09:00:01Z")
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"title": "Updated incident"},
            actor="editor",
        )
        for number, report_type in enumerate(REPORT_TYPES, start=2):
            close_ticket(service, ticket_id, 409)
            service.fix_clock(f"2026-10-05T09:00:0{number}Z")
            submit_report(service, ticket_id, report_type, actor=f"submitter-{number}")
            submit_report(service, ticket_id, report_type, 409)
        service.fix_clock("2026-10-05T09:00:05Z")
        close_ticket(service, ticket_id, actor="closer")
        close_ticket(service, ticket_id, 409)
        entries = audit_log(service, ticket_id)
        assert [entry["seq"] for entry in entries] == list(range(1, 8))
        assert [entry["action"] for entry in entries] == [
            "created",
            "triaged",
            "updated",
            *["report_submitted"] * 3,
            "closed",
        ]
        assert [entry["actor"] for entry in entries] == [
            "phase3-tests",
            "triager",
            "editor",
            "submitter-2",
            "submitter-3",
            "submitter-4",
            "closer",
        ]
        assert [entry["at"] for entry in entries] == [
            FIXED,
            FIXED,
            *[f"2026-10-05T09:00:0{number}Z" for number in range(1, 6)],
        ]
        assert entries[1]["reason"] == "Confirmed"
        assert all(entry["changes"] for entry in entries)
        assert REPORTER not in json.dumps(entries)
        before = closure_state(service, ticket_id)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            response = service.transport.send(
                method, f"/tickets/{ticket_id}/audit", body={"seq": 99, "action": "created"}
            )
            assert response.status == 405
            service.contract.validate("GET", "/tickets/{id}/audit", response)
            assert closure_state(service, ticket_id) == before
