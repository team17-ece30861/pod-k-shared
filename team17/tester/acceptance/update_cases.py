"""Portable Q6 cases: a PATCH that changes nothing succeeds and adds no audit entry.

Agreed with Team 21 on 2026-11-01. A real change still adds exactly one `updated`
entry, and a reporter-only change still counts (the reporter stays out of the log).
"""

import json
from copy import deepcopy

import pytest

from acceptance.draft_cases import FIELDS, FIXED, create_reportable
from acceptance.submission_cases import audit_log

NO_FIX = {key: value for key, value in deepcopy(FIELDS).items() if key != "fix"}


def patch(service, ticket_id, body, **kwargs):
    return service.expect(
        "PATCH", f"/tickets/{ticket_id}", 200, template="/tickets/{id}", body=body, **kwargs
    )


class UpdateAuditChecks:
    @pytest.fixture(autouse=True)
    def fresh_update_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    @pytest.mark.parametrize(
        "body",
        [
            {"title": "Authentication bypass"},
            {"product": {"name": "Router OS"}},
            {"product": {"manufacturer": "Acme", "affected_versions": ["4.1.0"]}},
            {"impact": None},
        ],
        ids=["same-title", "same-nested", "same-several", "null-for-unset"],
    )
    def test_q6_a_patch_that_changes_nothing_adds_no_audit_entry(self, service, body):
        ticket = service.create()
        before = audit_log(service, ticket["id"])
        assert patch(service, ticket["id"], body) == ticket
        assert audit_log(service, ticket["id"]) == before
        assert service.get(ticket["id"])["title"] == ticket["title"]

    @pytest.mark.parametrize(
        "body",
        [
            {"member_states": []},
            {"member_states": None},
            {"product": {"affected_versions": None}},
            {"product": {"affected_versions": []}},
        ],
        ids=["empty-list", "null-list", "null-nested-list", "empty-nested-list"],
    )
    def test_q6_an_empty_or_null_list_never_set_is_no_change(self, service, body):
        ticket = service.create(product={"manufacturer": "Acme", "name": "Router OS"})
        before = audit_log(service, ticket["id"])
        assert patch(service, ticket["id"], body) == ticket
        assert audit_log(service, ticket["id"]) == before

    def test_q6_same_awareness_with_a_reason_adds_no_audit_entry(self, service):
        ticket_id = create_reportable(service, **deepcopy(NO_FIX))
        before = audit_log(service, ticket_id)
        patch(service, ticket_id, {"aware_at": FIXED, "change_reason": "Rechecked"})
        assert audit_log(service, ticket_id) == before

    def test_a_real_change_still_adds_one_entry(self, service):
        ticket = service.create()
        before = audit_log(service, ticket["id"])
        patch(service, ticket["id"], {"title": "Renamed"}, actor="editor")
        entries = audit_log(service, ticket["id"])
        assert entries[:-1] == before
        assert entries[-1]["action"] == "updated"
        assert entries[-1]["actor"] == "editor"
        assert entries[-1]["changes"] == [
            {"field": "title", "old": "Authentication bypass", "new": "Renamed"}
        ]

    def test_a_reporter_only_change_adds_an_entry_without_the_reporter(self, service):
        ticket = service.create()
        secret = "q6-private-reporter@example.invalid"
        before = audit_log(service, ticket["id"])
        patch(service, ticket["id"], {"reporter": {"name": "New", "contact": secret}})
        entries = audit_log(service, ticket["id"])
        assert len(entries) == len(before) + 1
        assert entries[-1]["action"] == "updated"
        assert secret not in json.dumps(entries)
        assert service.get(ticket["id"])["reporter"]["contact"] == secret
