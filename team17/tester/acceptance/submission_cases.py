"""Portable AC-09 submission recording and failure/no-mutation cases."""

import json
from copy import deepcopy

import pytest

from acceptance.draft_cases import FIELDS, FIXED, REPORTER, create_reportable, read_draft
from acceptance.model_coverage import OracleChecks, model_scenario

REPORT_TYPES = ("early_warning", "notification", "final_report")


def submit_report(service, ticket_id, report_type="early_warning", status=200, body=None, **kwargs):
    return service.expect(
        "POST",
        f"/tickets/{ticket_id}/reports/{report_type}/submission",
        status,
        template="/tickets/{id}/reports/{type}/submission",
        body=body,
        **kwargs,
    )


def audit_log(service, ticket_id):
    return service.expect(
        "GET", f"/tickets/{ticket_id}/audit", 200, template="/tickets/{id}/audit", actor=None
    )


def observable_state(service, ticket_id):
    ticket = service.get(ticket_id)
    reports = {}
    if ticket["state"] == "reportable":
        reports = {report: read_draft(service, ticket_id, report) for report in REPORT_TYPES}
    return {"ticket": ticket, "audit": audit_log(service, ticket_id), "reports": reports}


def record_previous(service, ticket_id, report_type):
    for previous in REPORT_TYPES[: REPORT_TYPES.index(report_type)]:
        submit_report(service, ticket_id, previous)


class SubmissionChecks:
    @pytest.fixture(autouse=True)
    def fresh_submission_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    @pytest.mark.parametrize("report_type", REPORT_TYPES)
    def test_ac09_records_locked_content_and_one_audit_entry(self, service, report_type):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        record_previous(service, ticket_id, report_type)
        draft = read_draft(service, ticket_id, report_type)
        before = audit_log(service, ticket_id)
        recorded = submit_report(
            service,
            ticket_id,
            report_type,
            body={"reference": "receipt-17"},
            actor="recording-actor",
        )
        stored_draft = read_draft(service, ticket_id, report_type)
        entries = audit_log(service, ticket_id)
        model_checks = OracleChecks()
        model_checks.equal(
            "recorded report",
            recorded,
            {**draft, "status": "submitted", "submitted_at": FIXED, "reference": "receipt-17"},
        )
        model_checks.equal("stored draft vs recorded report", stored_draft, recorded)
        model_checks.equal("audit entry count", len(entries), len(before) + 1)
        model_checks.equal("earlier audit entries", entries[:-1], before)
        latest = entries[-1]
        model_checks.equal("audit seq", latest["seq"], len(entries))
        model_checks.equal("audit at", latest["at"], FIXED)
        model_checks.equal("audit actor", latest["actor"], "recording-actor")
        model_checks.equal("audit action", latest["action"], "report_submitted")
        model_checks.equal("audit reason", latest["reason"], None)
        model_checks.true("audit changes are listed", latest["changes"])
        model_checks.true(
            "report and audit leave out reporter details",
            REPORTER not in json.dumps({"recorded": recorded, "audit": entries}),
        )
        with model_scenario("A-05.record_" + report_type, comparisons=model_checks.results):
            assert all(model_checks.results), model_checks.summary()
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"product": {"name": "Post-record edit"}},
        )
        after_edit = read_draft(service, ticket_id, report_type)
        with model_scenario("C-03.locked_" + report_type):
            assert after_edit == recorded

    @pytest.mark.parametrize("body", [None, {}, {"submitted_at": FIXED}])
    def test_ac09_optional_body_defaults_to_clock_and_null_reference(self, service, body):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        recorded = submit_report(service, ticket_id, body=body)
        assert recorded["submitted_at"] == FIXED
        assert recorded["reference"] is None

    def test_ac09_full_recording_sequence_has_no_audit_gaps_after_failures(self, service):
        fields = deepcopy(FIELDS)
        fields.pop("fix")
        fields.pop("member_states")
        ticket_id = create_reportable(service, **fields)
        before = observable_state(service, ticket_id)
        submit_report(service, ticket_id, "notification", 409)
        missing = submit_report(service, ticket_id, status=422)
        assert "member_states" in {error["field"] for error in missing["errors"]}
        assert observable_state(service, ticket_id) == before
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"member_states": ["DE"]},
        )
        early = submit_report(service, ticket_id)
        before = observable_state(service, ticket_id)
        submit_report(service, ticket_id, status=409)
        submit_report(service, ticket_id, "final_report", 409)
        assert observable_state(service, ticket_id) == before
        submit_report(service, ticket_id, "notification")
        before = observable_state(service, ticket_id)
        missing_fix = submit_report(service, ticket_id, "final_report", 422)
        assert "fix.available_at" in {error["field"] for error in missing_fix["errors"]}
        assert observable_state(service, ticket_id) == before
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"fix": FIELDS["fix"]},
        )
        submit_report(service, ticket_id, "final_report")
        entries = audit_log(service, ticket_id)
        stored_early = read_draft(service, ticket_id, "early_warning")
        model_checks = OracleChecks()
        model_checks.equal(
            "audit seq values",
            [entry["seq"] for entry in entries],
            list(range(1, len(entries) + 1)),
        )
        model_checks.equal(
            "report_submitted audit entries",
            [entry["action"] for entry in entries].count("report_submitted"),
            3,
        )
        model_checks.equal(
            "stored early_warning content", stored_early["content"], early["content"]
        )
        with model_scenario("A-05.missing_fields_sequence", comparisons=model_checks.results):
            assert all(model_checks.results), model_checks.summary()

    @pytest.mark.parametrize("report_type", REPORT_TYPES)
    def test_ac09_duplicate_precedes_missing_fields_and_future_time(self, service, report_type):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        record_previous(service, ticket_id, report_type)
        recorded = submit_report(service, ticket_id, report_type)
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"member_states": None},
        )
        before = observable_state(service, ticket_id)
        submit_report(
            service, ticket_id, report_type, 409, body={"submitted_at": "2026-10-05T09:00:01Z"}
        )
        after = observable_state(service, ticket_id)
        stored = read_draft(service, ticket_id, report_type)
        model_checks = OracleChecks()
        model_checks.equal("ticket, reports and audit after the duplicate", after, before)
        model_checks.equal("stored report after the duplicate", stored, recorded)
        with model_scenario("A-05.duplicate_" + report_type, comparisons=model_checks.results):
            assert all(model_checks.results), model_checks.summary()

    @pytest.mark.parametrize("report_type", ["notification", "final_report"])
    def test_ac09_predecessor_conflict_precedes_missing_fields_and_future_time(
        self, service, report_type
    ):
        ticket_id = create_reportable(service)
        before = observable_state(service, ticket_id)
        submit_report(
            service, ticket_id, report_type, 409, body={"submitted_at": "2026-10-05T09:00:01Z"}
        )
        after = observable_state(service, ticket_id)
        with model_scenario("A-05.predecessor_" + report_type):
            assert after == before

    @pytest.mark.parametrize("state", ["received", "invalid", "not_actively_exploited"])
    @pytest.mark.parametrize("report_type", REPORT_TYPES)
    def test_ac09_nonreportable_tickets_reject_recording_without_changes(
        self, service, state, report_type
    ):
        ticket = service.create()
        if state != "received":
            service.triage(ticket["id"], {"decision": state, "reason": "Reviewed"})
        before = observable_state(service, ticket["id"])
        submit_report(
            service, ticket["id"], report_type, 409, body={"submitted_at": "2026-10-05T09:00:01Z"}
        )
        after = observable_state(service, ticket["id"])
        with model_scenario(f"A-05.state_{report_type}_{state}"):
            assert after == before
        if state != "received":
            with model_scenario(f"A-03.submission_{report_type}_{state}"):
                assert after == before

    @pytest.mark.parametrize(
        "body,field",
        [
            ({"submitted_at": None}, "submitted_at"),
            ({"submitted_at": "2026-10-05T09:00Z"}, "submitted_at"),
            ({"submitted_at": "2026-10-05T09:00:00"}, "submitted_at"),
            ({"reference": None}, "reference"),
            ({"reference": ""}, "reference"),
            ({"reference": "x" * 201}, "reference"),
            ({"reference": 17}, "reference"),
            ({"content": {"reporter": REPORTER}}, "content"),
            ({"reporter": REPORTER}, "reporter"),
        ],
        ids=[
            "null-time",
            "missing-seconds",
            "missing-offset",
            "null-reference",
            "empty-reference",
            "long-reference",
            "numeric-reference",
            "client-content",
            "private-field",
        ],
    )
    def test_ac09_schema_errors_precede_received_state_and_change_nothing(
        self, service, body, field
    ):
        ticket = service.create()
        before = observable_state(service, ticket["id"])
        problem = submit_report(service, ticket["id"], status=400, body=body)
        assert field in {error["field"] for error in problem["errors"]}
        assert REPORTER not in json.dumps(problem)
        assert observable_state(service, ticket["id"]) == before

    @pytest.mark.parametrize("raw", [b"null", b"[]", b"{bad"], ids=["null", "array", "malformed"])
    def test_ac09_invalid_json_body_shapes_preserve_ticket_and_audit(self, service, raw):
        ticket = service.create()
        before = observable_state(service, ticket["id"])
        submit_report(service, ticket["id"], status=400, raw=raw)
        assert observable_state(service, ticket["id"]) == before

    def test_ac09_body_validation_precedes_duplicate_conflict(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        submit_report(service, ticket_id)
        before = observable_state(service, ticket_id)
        submit_report(service, ticket_id, status=400, body={"reference": None})
        assert observable_state(service, ticket_id) == before

    @pytest.mark.parametrize("report_type", REPORT_TYPES)
    @pytest.mark.parametrize(
        "submitted_at,status",
        [
            ("2026-10-05T08:59:59Z", 422),
            (FIXED, 200),
            ("2026-10-08T09:00:00Z", 200),
            ("2026-10-08T09:00:01Z", 422),
        ],
        ids=["before-awareness", "at-awareness", "at-now", "after-now"],
    )
    def test_ac09_submission_interval_is_inclusive(
        self, service, report_type, submitted_at, status
    ):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        record_previous(service, ticket_id, report_type)
        service.fix_clock("2026-10-08T09:00:00Z")
        before = observable_state(service, ticket_id)
        result = submit_report(
            service, ticket_id, report_type, status, body={"submitted_at": submitted_at}
        )
        boundary = {
            "2026-10-05T08:59:59Z": "before_awareness",
            FIXED: "at_awareness",
            "2026-10-08T09:00:00Z": "at_now",
            "2026-10-08T09:00:01Z": "after_now",
        }[submitted_at]
        after = observable_state(service, ticket_id) if status == 422 else None
        model_checks = OracleChecks()
        if status == 422:
            fields = sorted({error["field"] for error in result["errors"]})
            model_checks.true(f"errors name submitted_at (got {fields})", "submitted_at" in fields)
            model_checks.equal("ticket, reports and audit after the rejection", after, before)
        else:
            model_checks.equal("submitted_at", result["submitted_at"], submitted_at)
        scenario = f"A-05.{report_type}_{boundary}"
        with model_scenario(scenario, comparisons=model_checks.results):
            assert all(model_checks.results), model_checks.summary()

    def test_ac09_reports_missing_fields_and_time_together(self, service):
        fields = deepcopy(FIELDS)
        fields.pop("member_states")
        ticket_id = create_reportable(service, **fields)
        before = observable_state(service, ticket_id)
        problem = submit_report(
            service, ticket_id, status=422, body={"submitted_at": "2026-10-05T09:00:01Z"}
        )
        assert {error["field"] for error in problem["errors"]} == {"member_states", "submitted_at"}
        assert observable_state(service, ticket_id) == before

    def test_ac14_recorded_time_normalizes_offset_and_truncates_fractions(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        recorded = submit_report(
            service, ticket_id, body={"submitted_at": "2026-10-05T11:00:00.999+02:00"}
        )
        assert recorded["submitted_at"] == FIXED

    def test_ac09_only_published_timestamp_interval_is_enforced_for_q5(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        service.fix_clock("2026-10-09T09:00:00Z")
        early = submit_report(service, ticket_id, body={"submitted_at": "2026-10-07T09:00:00Z"})
        notification = submit_report(
            service, ticket_id, "notification", body={"submitted_at": "2026-10-06T09:00:00Z"}
        )
        assert early["status"] == "submitted_late"
        assert notification["status"] == "submitted"

    def test_ac09_default_time_before_awareness_after_clock_reset_is_rejected(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        service.fix_clock("2026-10-05T08:59:59Z")
        before = observable_state(service, ticket_id)
        submit_report(service, ticket_id, status=422)
        assert observable_state(service, ticket_id) == before

    def test_ac09_audit_uses_recording_time_not_backdated_submission_time(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        service.fix_clock("2026-10-09T09:00:00Z")
        recorded = submit_report(service, ticket_id, body={"submitted_at": FIXED})
        assert recorded["submitted_at"] == FIXED
        assert audit_log(service, ticket_id)[-1]["at"] == "2026-10-09T09:00:00Z"

    @pytest.mark.parametrize("report_type", ["user_advisory", "unknown"])
    def test_ac09_non_submittable_types_precede_bad_body_and_missing_actor(
        self, service, report_type
    ):
        ticket = service.create()
        before = observable_state(service, ticket["id"])
        submit_report(service, ticket["id"], report_type, 404, raw=b"{bad", actor=None)
        after = observable_state(service, ticket["id"])
        assert after == before
        if report_type == "user_advisory":
            with model_scenario("C-04.advisory_recording"):
                assert after == before

    @pytest.mark.parametrize("ticket_id", ["not-an-id", "00000000-0000-0000-0000-000000000000"])
    def test_ac09_unknown_ticket_precedes_bad_body_and_missing_actor(self, service, ticket_id):
        submit_report(service, ticket_id, status=404, raw=b"{bad", actor=None)
        assert service.list() == []

    @pytest.mark.parametrize("actor", [None, "", "   "], ids=["missing", "empty", "blank"])
    def test_ac15_actor_is_required_and_preserves_all_data(self, service, actor):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        before = observable_state(service, ticket_id)
        submit_report(service, ticket_id, status=400, actor=actor)
        assert observable_state(service, ticket_id) == before

    @pytest.mark.parametrize(
        "authenticated,token,status",
        [(False, None, 401), (True, "wrong-token", 401), (True, None, 413)],
    )
    def test_ac15_auth_and_body_size_precede_unknown_path(
        self, service, authenticated, token, status
    ):
        submit_report(
            service,
            "not-an-id",
            "unknown",
            status,
            raw=b"x" * 65537,
            authenticated=authenticated,
            token=token,
            actor=None,
        )
        assert service.list() == []

    def test_ac09_locked_content_survives_product_edits(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        recorded = submit_report(service, ticket_id)
        service.expect(
            "PATCH",
            f"/tickets/{ticket_id}",
            200,
            template="/tickets/{id}",
            body={"product": {"name": "Changed"}},
        )
        early_warning = read_draft(service, ticket_id, "early_warning")
        notification = read_draft(service, ticket_id, "notification")
        model_checks = OracleChecks()
        model_checks.equal("recorded early_warning after the edit", early_warning, recorded)
        model_checks.equal(
            "notification product name", notification["content"]["product"]["name"], "Changed"
        )
        with model_scenario("C-03.product_edit", comparisons=model_checks.results):
            assert all(model_checks.results), model_checks.summary()

    def test_ac17_reset_deletes_actual_recorded_submissions(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        submit_report(service, ticket_id)
        service.expect("POST", "/test/reset", 204, actor=None)
        assert service.list() == []
        read_draft(service, ticket_id, "early_warning", 404)
