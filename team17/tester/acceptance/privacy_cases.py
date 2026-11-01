"""Portable AC-16 (REQ-15): the reporter appears only in GET /tickets/{id}.

One ticket goes through the whole lifecycle with a unique reporter string. Every other
response along the way, successes and errors alike, must not contain it.
"""

import json
from copy import deepcopy

import pytest

from acceptance.draft_cases import FIELDS, FIXED, read_draft
from acceptance.submission_cases import REPORT_TYPES, audit_log, submit_report

SECRET = "ac16-unique-reporter-4f1c9e@example.invalid"
SECRET_NAME = "AC16 Unique Reporter Name 4f1c9e"
LATER_SECRET = "ac16-changed-reporter-7b2d10@example.invalid"
REJECTED_SECRET = "ac16-rejected-reporter-93e5aa@example.invalid"
SECRETS = (SECRET, SECRET_NAME, LATER_SECRET, REJECTED_SECRET)


class PrivacyChecks:
    @pytest.fixture(autouse=True)
    def fresh_privacy_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    def test_ac16_reporter_appears_only_in_the_single_ticket_response(self, service):
        seen = []

        def call(method, path, status, template, **kwargs):
            body = service.expect(method, path, status, template=template, **kwargs)
            seen.append((f"{method} {path} -> {status}", body))
            return body

        fields = deepcopy(FIELDS)
        fields["reporter"] = {"name": SECRET_NAME, "contact": SECRET}
        ticket = service.create(**fields)
        seen.append(("POST /tickets -> 201", ticket))
        tid = ticket["id"]
        call(
            "POST",
            "/tickets",
            400,
            "/tickets",
            body={
                **deepcopy(fields),
                "reporter": {"name": "R", "contact": REJECTED_SECRET},
                "unknown_field": 1,
            },
        )
        base = f"/tickets/{tid}"

        def listed(query):
            body = call("GET", "/tickets" + query, 200, "/tickets", actor=None)
            assert tid in {ticket["id"] for ticket in body}, f"{query or 'list'} dropped the ticket"

        def reads():
            listed("")
            listed("?state=" + service.get(tid)["state"])
            call("GET", base + "/audit", 200, "/tickets/{id}/audit", actor=None)

        reads()
        # Errors before the ticket is reportable.
        call(
            "GET", base + "/reports/early_warning", 409, "/tickets/{id}/reports/{type}", actor=None
        )
        call(
            "POST",
            base + "/reports/early_warning/submission",
            409,
            "/tickets/{id}/reports/{type}/submission",
        )
        call("POST", base + "/close", 409, "/tickets/{id}/close")
        call(
            "POST",
            base + "/triage",
            400,
            "/tickets/{id}/triage",
            body={"decision": "actively_exploited", "reason": "Confirmed", "extra": SECRET},
        )
        call(
            "POST",
            base + "/triage",
            422,
            "/tickets/{id}/triage",
            body={
                "decision": "actively_exploited",
                "reason": "x",
                "aware_at": "2027-01-01T00:00:00Z",
            },
        )

        call(
            "POST",
            base + "/triage",
            200,
            "/tickets/{id}/triage",
            body={"decision": "actively_exploited", "aware_at": FIXED, "reason": "Confirmed"},
        )
        listed("?obligation_status=pending")
        call("GET", "/tickets?state=bogus", 400, "/tickets", actor=None)
        call("GET", base, 401, "/tickets/{id}", authenticated=False)
        # A rule-breaking change that carries a reporter: the error must not quote it.
        call(
            "PATCH",
            base,
            422,
            "/tickets/{id}",
            body={
                "reporter": {"name": SECRET_NAME, "contact": REJECTED_SECRET},
                "fix": {"available_at": "2027-01-01T00:00:00Z"},
            },
        )
        for report in (*REPORT_TYPES, "user_advisory"):
            seen.append((f"draft {report}", read_draft(service, tid, report)))
        call("GET", base + "/reports/bogus", 404, "/tickets/{id}/reports/{type}", actor=None)
        call(
            "POST",
            base + "/reports/notification/submission",
            409,
            "/tickets/{id}/reports/{type}/submission",
        )
        call(
            "POST",
            base + "/reports/early_warning/submission",
            422,
            "/tickets/{id}/reports/{type}/submission",
            body={"submitted_at": "2027-01-01T00:00:00Z"},
        )
        call("PATCH", base, 400, "/tickets/{id}", body={"reporter": {"contact": SECRET, "x": 1}})
        call("PATCH", base, 400, "/tickets/{id}", body={"title": "No actor"}, actor=None)

        # A reporter-only change: the response and its audit entry must not show either value.
        call(
            "PATCH",
            base,
            200,
            "/tickets/{id}",
            body={"reporter": {"name": SECRET_NAME, "contact": LATER_SECRET}},
        )
        call("PATCH", base, 200, "/tickets/{id}", body={"title": "Renamed incident"})
        for report in REPORT_TYPES:
            seen.append((f"submit {report}", submit_report(service, tid, report)))
        call(
            "POST",
            base + "/reports/final_report/submission",
            409,
            "/tickets/{id}/reports/{type}/submission",
        )
        call("POST", base + "/close", 200, "/tickets/{id}/close")
        call("POST", base + "/close", 409, "/tickets/{id}/close")
        call(
            "PATCH",
            base,
            409,
            "/tickets/{id}",
            body={"title": "Closed", "reporter": {"name": "X", "contact": REJECTED_SECRET}},
        )
        for report in (*REPORT_TYPES, "user_advisory"):
            seen.append((f"draft after close {report}", read_draft(service, tid, report)))
        reads()
        seen.append(("audit", audit_log(service, tid)))

        for label, body in seen:
            text = json.dumps(body)
            for value in SECRETS:
                assert value not in text, f"{label} leaked the reporter"

        # Failed requests left the reporter as the last successful change set it.
        detail = service.get(tid)
        assert detail["reporter"] == {"name": SECRET_NAME, "contact": LATER_SECRET}
