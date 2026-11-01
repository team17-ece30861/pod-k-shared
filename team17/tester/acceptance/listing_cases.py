"""Portable AC-06 (statuses through E6) and AC-11 (listing filters and order) cases.

Expected values are worked out by hand from docs/spec.md: the early warning is due
24 h after awareness, the notification 72 h, the final report 14 days after the fix
time (Model A/B, Model D E2 and E6). Ties in the list go to the ticket received
first, then the ID (Q2, agreed with Team 21 on 2026-11-01).
"""

from copy import deepcopy

import pytest

from acceptance.draft_cases import FIELDS, FIXED, create_reportable, read_draft
from acceptance.submission_cases import REPORT_TYPES, submit_report

AWARE = FIXED  # 2026-10-05T09:00:00Z, as in Model D E2
EW_DUE = "2026-10-06T09:00:00Z"
ONE_SECOND_LATE = "2026-10-06T09:00:01Z"
NOTIFICATION_DUE = "2026-10-08T09:00:00Z"
NO_FIX = {key: value for key, value in deepcopy(FIELDS).items() if key != "fix"}
FIX = {
    "available_at": "2026-10-06T09:00:00Z",
    "description": "Authentication fixed",
    "fixed_versions": ["1.1"],
}
FINAL_DUE = "2026-10-20T09:00:00Z"  # 14 days after FIX["available_at"]


def statuses(ticket):
    return {report: ticket["obligations"][report]["status"] for report in REPORT_TYPES}


def list_tickets(service, query="", status=200):
    result = service.expect("GET", "/tickets" + query, status, template="/tickets", actor=None)
    if status == 200:
        assert all("reporter" not in ticket for ticket in result)
    return result


def update(service, ticket_id, body):
    return service.expect(
        "PATCH", f"/tickets/{ticket_id}", 200, template="/tickets/{id}", body=body
    )


def reportable_at(service, aware_at):
    ticket = service.create(**deepcopy(NO_FIX))
    service.triage(
        ticket["id"],
        {"decision": "actively_exploited", "aware_at": aware_at, "reason": "Confirmed"},
    )
    return ticket["id"]


class StatusChecks:
    @pytest.fixture(autouse=True)
    def fresh_status_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    def test_ac06_steps_through_e6_and_records_on_time_and_late(self, service):
        ticket_id = create_reportable(service, **deepcopy(NO_FIX))

        service.fix_clock(EW_DUE)
        ticket = service.get(ticket_id)
        assert statuses(ticket) == {
            "early_warning": "pending",
            "notification": "pending",
            "final_report": "awaiting_fix",
        }
        assert ticket["obligations"]["early_warning"]["due_at"] == EW_DUE
        assert ticket["obligations"]["final_report"]["due_at"] is None
        assert ticket["next_due_at"] == EW_DUE

        service.fix_clock(ONE_SECOND_LATE)
        assert statuses(service.get(ticket_id))["early_warning"] == "overdue"

        submit_report(service, ticket_id, "early_warning", body={"reference": "ew-17"})
        submit_report(service, ticket_id, "notification")
        ticket = service.get(ticket_id)
        assert statuses(ticket) == {
            "early_warning": "submitted_late",
            "notification": "submitted",
            "final_report": "awaiting_fix",
        }
        assert ticket["obligations"]["early_warning"] == {
            "status": "submitted_late",
            "due_at": EW_DUE,
            "submitted_at": ONE_SECOND_LATE,
            "reference": "ew-17",
        }
        assert ticket["obligations"]["notification"]["submitted_at"] == ONE_SECOND_LATE
        assert ticket["obligations"]["notification"]["reference"] is None
        # Recorded reports have no unmet deadline, and the final report has none yet.
        assert ticket["next_due_at"] is None

        ticket = update(service, ticket_id, {"fix": FIX})
        assert ticket["obligations"]["final_report"]["status"] == "pending"
        assert ticket["obligations"]["final_report"]["due_at"] == FINAL_DUE
        assert ticket["next_due_at"] == FINAL_DUE

        # Every view agrees: the list and each draft show the same obligation.
        assert list_tickets(service) == [ticket]
        for report in REPORT_TYPES:
            draft = read_draft(service, ticket_id, report)
            shown = {key: draft[key] for key in ("status", "due_at", "submitted_at", "reference")}
            assert shown == ticket["obligations"][report]

    def test_ac06_a_report_recorded_exactly_at_its_due_time_is_on_time(self, service):
        ticket_id = create_reportable(service, **deepcopy(NO_FIX))
        service.fix_clock("2026-10-06T09:00:05Z")
        recorded = submit_report(service, ticket_id, "early_warning", body={"submitted_at": EW_DUE})
        assert recorded["status"] == "submitted"
        assert statuses(service.get(ticket_id))["early_warning"] == "submitted"

    def test_ac06_moving_the_clock_back_does_not_change_a_recorded_status(self, service):
        ticket_id = create_reportable(service, **deepcopy(NO_FIX))
        service.fix_clock(ONE_SECOND_LATE)
        submit_report(service, ticket_id, "early_warning")
        service.fix_clock(FIXED)
        assert statuses(service.get(ticket_id))["early_warning"] == "submitted_late"

    def test_q4_an_earlier_awareness_can_make_a_recorded_report_late(self, service):
        ticket_id = create_reportable(service, **deepcopy(NO_FIX))
        service.fix_clock(ONE_SECOND_LATE)
        submit_report(service, ticket_id, "early_warning")
        submit_report(service, ticket_id, "notification")
        assert statuses(service.get(ticket_id))["notification"] == "submitted"

        # Notification due 72 h after 2026-10-03T09:00:00Z = 2026-10-06T09:00:00Z,
        # one second before it was recorded.
        ticket = update(
            service,
            ticket_id,
            {"aware_at": "2026-10-03T09:00:00Z", "change_reason": "Earlier evidence found"},
        )
        assert ticket["obligations"]["notification"]["due_at"] == "2026-10-06T09:00:00Z"
        assert ticket["obligations"]["notification"]["status"] == "submitted_late"
        assert read_draft(service, ticket_id, "notification")["status"] == "submitted_late"


class ListingChecks:
    @pytest.fixture(autouse=True)
    def fresh_listing_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    def four_tickets(self, service):
        """Created out of urgency order, so creation order can't pass the test."""
        later = reportable_at(service, AWARE)  # early warning due 2026-10-06T09:00:00Z
        received = service.create()["id"]
        overdue = reportable_at(service, "2026-10-04T08:00:00Z")  # due 2026-10-05T08:00:00Z
        sooner = reportable_at(service, "2026-10-05T08:00:00Z")  # due 2026-10-06T08:00:00Z
        return received, overdue, sooner, later

    def ids(self, tickets):
        return [ticket["id"] for ticket in tickets]

    def test_ac11_unfiltered_list_is_nearest_unmet_deadline_first(self, service):
        received, overdue, sooner, later = self.four_tickets(service)
        tickets = list_tickets(service)
        assert self.ids(tickets) == [overdue, sooner, later, received]
        assert [ticket["next_due_at"] for ticket in tickets] == [
            "2026-10-05T08:00:00Z",
            "2026-10-06T08:00:00Z",
            EW_DUE,
            None,
        ]

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("?state=received", ["received"]),
            ("?state=reportable", ["overdue", "sooner", "later"]),
            ("?state=closed", []),
            ("?obligation_status=overdue", ["overdue"]),
            # The overdue ticket's notification is still pending, so it matches too.
            ("?obligation_status=pending", ["overdue", "sooner", "later"]),
            ("?obligation_status=not_applicable", ["received"]),
            ("?obligation_status=awaiting_fix", ["overdue", "sooner", "later"]),
            ("?obligation_status=submitted", []),
            ("?state=reportable&obligation_status=overdue", ["overdue"]),
            ("?state=received&obligation_status=pending", []),
        ],
    )
    def test_ac11_filters_return_exactly_the_expected_tickets_in_order(
        self, service, query, expected
    ):
        received, overdue, sooner, later = self.four_tickets(service)
        names = {"received": received, "overdue": overdue, "sooner": sooner, "later": later}
        assert self.ids(list_tickets(service, query)) == [names[name] for name in expected]

    def test_ac11_status_filters_follow_recorded_reports(self, service):
        received, overdue, sooner, later = self.four_tickets(service)
        submit_report(service, overdue, "early_warning")  # recorded after its deadline
        submit_report(service, sooner, "early_warning")  # recorded before its deadline
        assert self.ids(list_tickets(service, "?obligation_status=submitted_late")) == [overdue]
        assert self.ids(list_tickets(service, "?obligation_status=submitted")) == [sooner]
        assert self.ids(list_tickets(service, "?obligation_status=overdue")) == []
        # The overdue ticket's next unmet deadline is now its notification, due
        # 2026-10-07T08:00:00Z; the sooner one's is 2026-10-08T08:00:00Z.
        assert self.ids(list_tickets(service)) == [later, overdue, sooner, received]

    def test_ac11_state_closed_returns_closed_tickets(self, service):
        received, overdue, sooner, later = self.four_tickets(service)
        closed = service.create()["id"]
        service.triage(closed, {"decision": "invalid", "reason": "Not a vulnerability"})
        assert self.ids(list_tickets(service, "?state=closed")) == [closed]
        # Both have no deadline and the same receipt time, so they share the end of the list.
        assert set(self.ids(list_tickets(service))[-2:]) == {received, closed}

    @pytest.mark.parametrize(
        "query",
        ["?state=open", "?state=", "?obligation_status=late", "?obligation_status=PENDING"],
    )
    def test_ac11_unknown_filter_values_are_rejected(self, service, query):
        self.four_tickets(service)
        list_tickets(service, query, status=400)

    def test_q2_ties_go_to_the_ticket_received_first(self, service):
        service.fix_clock("2026-10-05T07:00:00Z")
        first_received = service.create()["id"]
        first_reportable = service.create(**deepcopy(NO_FIX))["id"]
        service.fix_clock(FIXED)
        second_reportable = service.create(**deepcopy(NO_FIX))["id"]
        second_received = service.create()["id"]
        for ticket_id in (second_reportable, first_reportable):
            service.triage(
                ticket_id,
                {"decision": "actively_exploited", "aware_at": AWARE, "reason": "Confirmed"},
            )
        assert self.ids(list_tickets(service)) == [
            first_reportable,
            second_reportable,
            first_received,
            second_received,
        ]
