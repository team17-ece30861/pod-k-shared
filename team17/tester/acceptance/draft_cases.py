"""Portable live-draft cases for AC-08; locked snapshots need recorded submissions."""

import json
from copy import deepcopy

import pytest

FIXED = "2026-10-05T09:00:00Z"
REPORTER = "private-draft-reporter@example.invalid"
PRODUCT = {"manufacturer": "Acme", "name": "Router", "affected_versions": ["1.0"]}
FIELDS = {
    "product": PRODUCT,
    "member_states": ["DE"],
    "vulnerability": {"cve_id": "CVE-2026-12345", "description": "Authentication bypass"},
    "exploit": {"description": "Confirmed exploitation"},
    "mitigations": {"taken": "Disabled access", "for_users": "Install update"},
    "sensitivity": "AMBER",
    "severity": "high",
    "impact": "Unauthorized access",
    "malicious_actor": "Unknown actor",
    "fix": {
        "available_at": FIXED,
        "description": "Authentication fixed",
        "fixed_versions": ["1.1"],
    },
    "reporter": {"name": REPORTER, "contact": REPORTER},
}
EARLY_REQUIRED = [
    "product.manufacturer",
    "product.name",
    "product.affected_versions",
    "member_states",
]
NOTIFICATION_REQUIRED = [
    *EARLY_REQUIRED,
    "vulnerability.description",
    "exploit.description",
    "mitigations.taken",
    "mitigations.for_users",
    "sensitivity",
]
REQUIRED = {
    "early_warning": EARLY_REQUIRED,
    "notification": NOTIFICATION_REQUIRED,
    "final_report": [
        *NOTIFICATION_REQUIRED,
        "severity",
        "impact",
        "fix.available_at",
        "fix.description",
        "fix.fixed_versions",
    ],
    "user_advisory": [
        "product.manufacturer",
        "product.name",
        "product.affected_versions",
        "vulnerability.description",
        "mitigations.for_users",
    ],
}
CONTENT = {
    "early_warning": {"product": PRODUCT, "member_states": ["DE"]},
    "notification": {
        "product": PRODUCT,
        "member_states": ["DE"],
        "vulnerability": FIELDS["vulnerability"],
        "exploit": FIELDS["exploit"],
        "mitigations": FIELDS["mitigations"],
        "sensitivity": "AMBER",
    },
    "final_report": {
        "product": PRODUCT,
        "member_states": ["DE"],
        "vulnerability": FIELDS["vulnerability"],
        "exploit": FIELDS["exploit"],
        "mitigations": FIELDS["mitigations"],
        "sensitivity": "AMBER",
        "severity": "high",
        "impact": "Unauthorized access",
        "malicious_actor": "Unknown actor",
        "fix": FIELDS["fix"],
    },
    "user_advisory": {
        "product": PRODUCT,
        "vulnerability": FIELDS["vulnerability"],
        "mitigations": {"for_users": "Install update"},
        "severity": "high",
        "impact": "Unauthorized access",
        "fix": FIELDS["fix"],
    },
}
DUE = {
    "early_warning": "2026-10-06T09:00:00Z",
    "notification": "2026-10-08T09:00:00Z",
    "final_report": "2026-10-19T09:00:00Z",
    "user_advisory": None,
}


def create_reportable(service, **fields):
    ticket = service.create(**fields)
    service.triage(
        ticket["id"], {"decision": "actively_exploited", "aware_at": FIXED, "reason": "Confirmed"}
    )
    return ticket["id"]


def read_draft(service, ticket_id, report_type, status=200, **kwargs):
    return service.expect(
        "GET",
        f"/tickets/{ticket_id}/reports/{report_type}",
        status,
        template="/tickets/{id}/reports/{type}",
        actor=None,
        **kwargs,
    )


class DraftChecks:
    @pytest.fixture(autouse=True)
    def fresh_draft_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    @pytest.mark.parametrize("report_type", REQUIRED)
    def test_ac08_complete_live_draft_matches_model_c(self, service, report_type):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        draft = read_draft(service, ticket_id, report_type)
        assert draft == {
            "ticket_id": ticket_id,
            "type": report_type,
            "status": None if report_type == "user_advisory" else "pending",
            "due_at": DUE[report_type],
            "submitted_at": None,
            "reference": None,
            "content": {"ticket_id": ticket_id, "aware_at": FIXED, **CONTENT[report_type]},
            "missing_fields": [],
        }
        assert REPORTER not in json.dumps(draft)

    def test_ac08_missing_fields_follow_each_ticket_update(self, service):
        ticket_id = create_reportable(
            service,
            product={"manufacturer": "Acme", "name": "Router"},
            reporter={"name": REPORTER, "contact": REPORTER},
        )
        filled = {"product.manufacturer", "product.name"}
        patches = [
            ("product.affected_versions", {"product": {"affected_versions": ["1.0"]}}),
            ("member_states", {"member_states": ["DE"]}),
            ("vulnerability.description", {"vulnerability": {"description": "Description"}}),
            ("exploit.description", {"exploit": {"description": "Exploited"}}),
            ("mitigations.taken", {"mitigations": {"taken": "Restricted"}}),
            ("mitigations.for_users", {"mitigations": {"for_users": "Update"}}),
            ("sensitivity", {"sensitivity": "GREEN"}),
            ("severity", {"severity": "high"}),
            ("impact", {"impact": "Impact"}),
            ("fix.available_at", {"fix": {"available_at": FIXED}}),
            ("fix.description", {"fix": {"description": "Fix"}}),
            ("fix.fixed_versions", {"fix": {"fixed_versions": ["1.1"]}}),
        ]
        for field, patch in [(None, None), *patches]:
            if patch is not None:
                service.expect(
                    "PATCH", f"/tickets/{ticket_id}", 200, template="/tickets/{id}", body=patch
                )
                filled.add(field)
            for report_type, required in REQUIRED.items():
                draft = read_draft(service, ticket_id, report_type)
                assert draft["missing_fields"] == [
                    field for field in required if field not in filled
                ]
                assert REPORTER not in json.dumps(draft)

    def test_ac08_repeated_reads_leave_ticket_and_audit_unchanged(self, service):
        ticket_id = create_reportable(service, **deepcopy(FIELDS))
        before = service.get(ticket_id)
        audit_path = f"/tickets/{ticket_id}/audit"
        audit = service.expect("GET", audit_path, 200, template="/tickets/{id}/audit")
        for report_type in REQUIRED:
            assert read_draft(service, ticket_id, report_type) == read_draft(
                service, ticket_id, report_type
            )
        assert service.get(ticket_id) == before
        assert service.expect("GET", audit_path, 200, template="/tickets/{id}/audit") == audit

    @pytest.mark.parametrize("report_type", REQUIRED)
    @pytest.mark.parametrize("state", ["received", "invalid", "not_actively_exploited"])
    def test_ac08_never_reportable_ticket_returns_409(self, service, report_type, state):
        ticket = service.create()
        if state != "received":
            service.triage(ticket["id"], {"decision": state, "reason": "Reviewed"})
        before = service.get(ticket["id"])
        read_draft(service, ticket["id"], report_type, 409)
        assert service.get(ticket["id"]) == before

    @pytest.mark.parametrize("unknown_id", ["00000000-0000-0000-0000-000000000000", "not-an-id"])
    def test_ac08_unknown_ticket_returns_404(self, service, unknown_id):
        read_draft(service, unknown_id, "early_warning", 404)
        assert service.list() == []

    @pytest.mark.parametrize("report_type", ["unknown", "EARLY_WARNING", "advisory"])
    def test_ac08_unknown_report_type_precedes_nonreportable_state(self, service, report_type):
        ticket = service.create()
        before = service.get(ticket["id"])
        read_draft(service, ticket["id"], report_type, 404)
        assert service.get(ticket["id"]) == before

    @pytest.mark.parametrize("report_type", REQUIRED)
    @pytest.mark.parametrize("authenticated,token", [(False, None), (True, "wrong-draft-token")])
    def test_ac15_all_drafts_require_auth(self, service, report_type, authenticated, token):
        read_draft(service, "not-an-id", report_type, 401, authenticated=authenticated, token=token)

    def test_ac06_draft_statuses_follow_current_clock_without_mutating_ticket(self, service):
        ticket_id = create_reportable(service)
        service.fix_clock("2026-10-06T09:00:00Z")
        assert read_draft(service, ticket_id, "early_warning")["status"] == "pending"
        service.fix_clock("2026-10-06T09:00:01Z")
        assert read_draft(service, ticket_id, "early_warning")["status"] == "overdue"
        assert read_draft(service, ticket_id, "notification")["status"] == "pending"
        final = read_draft(service, ticket_id, "final_report")
        assert final["status"] == "awaiting_fix"
        assert final["due_at"] is None
        advisory = read_draft(service, ticket_id, "user_advisory")
        assert advisory["status"] is advisory["due_at"] is None
