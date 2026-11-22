"""Portable Phase 5 update, error-precedence and clock-release checks (5.10)."""

import time
from datetime import datetime
from urllib.parse import quote

import pytest

from acceptance.draft_cases import FIXED, create_reportable
from acceptance.submission_cases import audit_log, observable_state

WRITE_ROUTES = [
    ("PATCH", "/tickets/{id}", {"title": "Changed"}),
    (
        "POST",
        "/tickets/{id}/triage",
        {"decision": "actively_exploited", "aware_at": FIXED, "reason": "Confirmed"},
    ),
]
CLOCK_SKEW_SECONDS = 120
RECEIPT_SLACK_SECONDS = 1


def observation(service, ticket_id):
    return {**observable_state(service, ticket_id), "tickets": service.list()}


class HttpGapChecks:
    @pytest.fixture(autouse=True)
    def fresh_http_gap_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    def test_ac07_awareness_edit_recalculates_all_deadlines_and_records_audit(self, service):
        ticket_id = create_reportable(service, fix={"available_at": "2026-10-04T09:00:00Z"})
        before = service.get(ticket_id)
        entries_before = audit_log(service, ticket_id)
        changed = service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            actor="awareness-editor",
            body={
                "aware_at": "2026-10-03T09:00:00Z",
                "change_reason": "Earlier evidence",
                "product": {"name": "Renamed router"},
            },
        )
        assert changed["aware_at"] == "2026-10-03T09:00:00Z"
        assert changed["product"] == {**before["product"], "name": "Renamed router"}
        assert changed["fix"] == before["fix"]
        expected = {
            "early_warning": ("2026-10-04T09:00:00Z", "overdue"),
            "notification": ("2026-10-06T09:00:00Z", "pending"),
            "final_report": ("2026-10-18T09:00:00Z", "pending"),
        }
        for report, (due, status) in expected.items():
            assert changed["obligations"][report]["due_at"] == due
            assert changed["obligations"][report]["status"] == status
        assert changed["next_due_at"] == "2026-10-04T09:00:00Z"
        assert service.get(ticket_id) == {**changed, "reporter": before["reporter"]}
        listed = next(ticket for ticket in service.list() if ticket["id"] == ticket_id)
        assert listed["next_due_at"] == "2026-10-04T09:00:00Z"
        entries = audit_log(service, ticket_id)
        assert entries[:-1] == entries_before
        assert len(entries) == len(entries_before) + 1
        assert entries[-1]["seq"] == len(entries)
        assert entries[-1]["at"] == FIXED
        assert entries[-1]["actor"] == "awareness-editor"
        assert entries[-1]["action"] == "updated"
        assert entries[-1]["reason"] == "Earlier evidence"
        # The contract types changes[].field only as a string: how nested fields are named,
        # and whether recalculated deadlines are listed too, are left to each system.
        assert any(
            (change["field"], change["old"], change["new"])
            == ("aware_at", FIXED, "2026-10-03T09:00:00Z")
            for change in entries[-1]["changes"]
        )

    def test_ac07_setting_fix_time_immediately_creates_final_deadline(self, service):
        ticket_id = create_reportable(service)
        before = audit_log(service, ticket_id)
        assert service.get(ticket_id)["obligations"]["final_report"]["status"] == "awaiting_fix"
        service.fix_clock("2026-10-06T09:00:00Z")
        changed = service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"fix": {"available_at": "2026-10-06T09:00:00Z"}},
        )
        assert changed["obligations"]["final_report"]["due_at"] == "2026-10-20T09:00:00Z"
        assert changed["obligations"]["final_report"]["status"] == "pending"
        assert service.get(ticket_id)["obligations"] == changed["obligations"]
        entries = audit_log(service, ticket_id)
        assert entries[:-1] == before
        assert len(entries) == len(before) + 1
        assert entries[-1]["action"] == "updated"
        assert entries[-1]["at"] == "2026-10-06T09:00:00Z"

    @pytest.mark.parametrize(
        "reportable,body,status,fields",
        [
            # The contract doesn't say which field a missing change_reason names.
            (True, {"aware_at": "2026-10-04T09:00:00Z"}, 422, {"change_reason", "aware_at"}),
            (
                True,
                {"aware_at": "2026-10-05T09:00:01Z", "change_reason": "Future evidence"},
                422,
                {"aware_at"},
            ),
            (True, {"fix": {"available_at": "2026-10-05T09:00:01Z"}}, 422, {"fix.available_at"}),
            (
                False,
                {"aware_at": "2026-10-04T09:00:00Z", "change_reason": "Before triage"},
                409,
                None,
            ),
        ],
        ids=["missing-reason", "future-awareness", "future-fix", "received-awareness"],
    )
    def test_ac07_rejected_edit_preserves_ticket_drafts_list_and_audit(
        self, service, reportable, body, status, fields
    ):
        ticket_id = create_reportable(service) if reportable else service.create()["id"]
        before = observation(service, ticket_id)
        problem = service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            status,
            template="/tickets/{id}",
            body={"title": "Must not persist", **body},
        )
        if fields is not None:
            assert fields & {error["field"] for error in problem["errors"]}
        assert observation(service, ticket_id) == before

    @pytest.mark.parametrize("method,template,body", WRITE_ROUTES, ids=["patch", "triage"])
    @pytest.mark.parametrize("unknown_id", ["00000000-0000-0000-0000-000000000000", "not-an-id"])
    @pytest.mark.parametrize(
        "invalid_request", [False, True], ids=["valid-body", "bad-body-no-actor"]
    )
    def test_unknown_write_target_returns_404_before_request_validation(
        self, service, method, template, body, unknown_id, invalid_request
    ):
        ticket_id = service.create()["id"]
        assert ticket_id != unknown_id
        before = observation(service, ticket_id)
        arguments = {"raw": b"{bad", "actor": None} if invalid_request else {"body": body}
        service.expect(
            method,
            template.replace("{id}", quote(unknown_id, safe="")),
            404,
            template=template,
            **arguments,
        )
        assert observation(service, ticket_id) == before

    @pytest.mark.parametrize("method,template,body", WRITE_ROUTES, ids=["patch", "triage"])
    @pytest.mark.parametrize(
        "unknown_target", [False, True], ids=["existing-target", "unknown-target"]
    )
    def test_oversized_write_returns_413_before_not_found_actor_and_json_validation(
        self, service, method, template, body, unknown_target
    ):
        ticket_id = service.create()["id"]
        before = observation(service, ticket_id)
        target = "not-an-id" if unknown_target else ticket_id
        service.expect(
            method,
            template.replace("{id}", quote(target, safe="")),
            413,
            template=template,
            raw=b"x" * 65537,
            actor=None,
        )
        assert observation(service, ticket_id) == before

    def test_delete_clock_returns_204_and_restores_advancing_receipt_time(self, service):
        ticket_id = service.create()["id"]
        before = observation(service, ticket_id)
        try:
            real_before = time.time()
            service.expect("DELETE", "/test/clock", 204, actor=None)
            first = service.expect("GET", "/test/clock", 200, actor=None)["now"]
            created = service.create()
            time.sleep(2.1)
            second = service.expect("GET", "/test/clock", 200, actor=None)["now"]
            real_after = time.time()
            first, second, received = (
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                for value in (first, second, created["received_at"])
            )
            for returned_seconds in (first, second, received):
                assert real_before - CLOCK_SKEW_SECONDS <= returned_seconds
                assert returned_seconds <= real_after + CLOCK_SKEW_SECONDS
            assert first < second
            # Times are truncated to the second, and the clock read and the ticket's
            # receipt are separate requests, so allow a second either side.
            assert first - RECEIPT_SLACK_SECONDS <= received <= second + RECEIPT_SLACK_SECONDS
            assert service.get(ticket_id) == before["ticket"]
            assert audit_log(service, ticket_id) == before["audit"]
            assert {ticket["id"] for ticket in service.list()} == {ticket_id, created["id"]}
        finally:
            service.fix_clock(FIXED)
