"""Phase 3 cases shared by Django integration and external HTTP runs."""

import json
import time
from urllib.parse import quote

import pytest

from acceptance.client import ROOT, Contract

FIXED = "2026-10-05T09:00:00Z"
MODEL_D = json.loads((ROOT / "tests/fixtures/phase3_model_d.json").read_text(encoding="utf-8"))[
    "cases"
]
PROTECTED = [
    (method.upper(), path)
    for path, item in Contract().document["paths"].items()
    for method in item
    if method in {"get", "post", "put", "patch", "delete"} and (method, path) != ("get", "/health")
]


class Phase3Checks:
    @pytest.fixture(autouse=True)
    def fresh_service(self, service):
        service.expect("POST", "/test/reset", 204, actor=None)
        service.fix_clock(FIXED)

    def test_ac01_intake_has_unique_ids_and_fixed_receipt_time(self, service):
        tickets = [service.create(), service.create()]
        assert tickets[0]["id"] != tickets[1]["id"]
        for ticket in tickets:
            assert ticket["state"] == "received"
            assert ticket["received_at"] == FIXED
            assert ticket["aware_at"] is None
            assert all(
                value["status"] == "not_applicable" for value in ticket["obligations"].values()
            )
            assert service.get(ticket["id"])["reporter"]["contact"] == "reporter@example.invalid"

    @pytest.mark.parametrize(
        "change,field",
        [
            ({"title": None}, "title"),
            ({"source": "email"}, "source"),
            ({"received_at": FIXED}, "received_at"),
        ],
        ids=["missing-title", "unknown-source", "system-field"],
    )
    def test_ac02_invalid_intake_preserves_ticket_count(self, service, change, field):
        existing = service.create()
        body = {
            "source": "internal",
            "title": "Test",
            "description": "Test",
            "product": {"manufacturer": "Acme", "name": "Router"},
            **change,
        }
        if change.get("title", "present") is None:
            body.pop("title")
        problem = service.expect("POST", "/tickets", 400, body=body)
        assert field in {error["field"] for error in problem["errors"]}
        assert [ticket["id"] for ticket in service.list()] == [existing["id"]]

    @pytest.mark.parametrize(
        "raw,status", [(b"{bad", 400), (b"x" * 65537, 413)], ids=["invalid-json", "over-64-kb"]
    )
    def test_ac02_malformed_intake_preserves_ticket_count(self, service, raw, status):
        service.expect("POST", "/tickets", status, raw=raw)
        assert service.list() == []

    @pytest.mark.parametrize("case", MODEL_D, ids=[case["id"] for case in MODEL_D])
    def test_ac03_model_d_exact_deadlines_and_statuses(self, service, case):
        service.fix_clock(
            case.get("received_at")
            or max(case["aware_at"], case.get("fix_available_at", case["aware_at"]))
        )
        fields = (
            {"fix": {"available_at": case["fix_available_at"]}}
            if "fix_available_at" in case
            else {}
        )
        ticket = service.create(**fields)
        service.fix_clock(
            case.get("now") or max(case["aware_at"], case.get("fix_available_at", case["aware_at"]))
        )
        result = service.triage(
            ticket["id"],
            {
                "decision": "actively_exploited",
                "aware_at": case["aware_at"],
                "reason": "Simulated confirmed exploitation.",
            },
        )
        assert result["state"] == "reportable"
        assert result["aware_at"] == case["aware_at"]
        for report, due_at in case["expected_deadlines"].items():
            assert result["obligations"][report]["due_at"] == due_at
        for report, status in case.get("expected_statuses", {}).items():
            assert result["obligations"][report]["status"] == status

    @pytest.mark.parametrize(
        "body,status,field",
        [
            ({"decision": "actively_exploited", "reason": "Confirmed"}, 400, "aware_at"),
            (
                {
                    "decision": "actively_exploited",
                    "reason": "Confirmed",
                    "aware_at": "2026-10-05T09:00:01Z",
                },
                422,
                "aware_at",
            ),
            ({"decision": "actively_exploited", "aware_at": FIXED}, 400, "reason"),
        ],
        ids=["missing-awareness", "future-awareness", "missing-reason"],
    )
    def test_ac04_rejected_triage_preserves_ticket(self, service, body, status, field):
        ticket = service.create()
        before = service.get(ticket["id"])
        problem = service.triage(ticket["id"], body, status)
        assert field in {error["field"] for error in problem["errors"]}
        assert service.get(ticket["id"]) == before

    def test_ac04_retriage_is_conflict_and_changes_nothing(self, service):
        ticket = service.create()
        body = {"decision": "actively_exploited", "aware_at": FIXED, "reason": "Confirmed"}
        service.triage(ticket["id"], body)
        before = service.get(ticket["id"])
        service.triage(ticket["id"], body, 409)
        assert service.get(ticket["id"]) == before

    @pytest.mark.parametrize("decision", ["not_actively_exploited", "invalid"])
    def test_ac05_closed_ticket_remains_readable_and_rejects_changes(self, service, decision):
        ticket = service.create()
        result = service.triage(ticket["id"], {"decision": decision, "reason": "Reviewed"})
        assert result["state"] == "closed"
        assert result["resolution"] == decision
        assert all(
            value["status"] == "not_applicable" and value["due_at"] is None
            for value in result["obligations"].values()
        )
        before = service.get(ticket["id"])
        service.expect(
            "PATCH",
            "/tickets/" + quote(ticket["id"], safe=""),
            409,
            template="/tickets/{id}",
            body={"title": "Must not change"},
        )
        assert service.get(ticket["id"]) == before

    @pytest.mark.parametrize(
        "aware_at",
        ["2026-10-05T11:00:00+02:00", "2026-10-05T09:00:00.789Z"],
        ids=["E7-offset", "fraction-truncation"],
    )
    def test_ac14_timestamps_are_normalized(self, service, aware_at):
        ticket = service.create()
        result = service.triage(
            ticket["id"],
            {"decision": "actively_exploited", "aware_at": aware_at, "reason": "Confirmed"},
        )
        assert result["aware_at"] == FIXED
        assert result["obligations"]["early_warning"]["due_at"] == "2026-10-06T09:00:00Z"

    @pytest.mark.parametrize(
        "aware_at",
        ["2026-10-05 09:00", "2026-10-05T09:00Z", "2026-10-05T09:00:00"],
        ids=["bad-format", "missing-seconds", "missing-offset"],
    )
    def test_ac14_bad_timestamp_leaves_ticket_unchanged(self, service, aware_at):
        ticket = service.create()
        before = service.get(ticket["id"])
        service.triage(
            ticket["id"],
            {"decision": "actively_exploited", "aware_at": aware_at, "reason": "Confirmed"},
            400,
        )
        assert service.get(ticket["id"]) == before

    @pytest.mark.parametrize(
        "method,path", PROTECTED, ids=[method + " " + path for method, path in PROTECTED]
    )
    @pytest.mark.parametrize(
        "authenticated,token",
        [(False, None), (True, "invalid-acceptance-token")],
        ids=["no-token", "wrong-token"],
    )
    def test_ac15_every_protected_operation_rejects_unauthorized_requests(
        self, service, method, path, authenticated, token
    ):
        actual = path.replace("{id}", "unknown-ticket").replace("{type}", "early_warning")
        service.expect(method, actual, 401, template=path, authenticated=authenticated, token=token)

    def test_ac15_health_is_public(self, service):
        service.expect("GET", "/health", 200, authenticated=False, actor=None)

    def test_ac15_missing_actor_changes_nothing(self, service):
        service.expect(
            "POST",
            "/tickets",
            400,
            actor=None,
            body={
                "source": "internal",
                "title": "Test",
                "description": "Test",
                "product": {"manufacturer": "Acme", "name": "Router"},
            },
        )
        assert service.list() == []

    def test_ac17_reset_deletes_tickets(self, service):
        ticket = service.create()
        service.expect("POST", "/test/reset", 204, actor=None)
        assert service.list() == []
        service.expect(
            "GET", "/tickets/" + quote(ticket["id"], safe=""), 404, template="/tickets/{id}"
        )

    def test_ac17_clock_stays_fixed_and_controls_receipt(self, service):
        first = service.expect("GET", "/test/clock", 200, actor=None)
        time.sleep(2)
        assert service.expect("GET", "/test/clock", 200, actor=None) == first == {"now": FIXED}
        assert service.create()["received_at"] == FIXED

    def test_invalid_clock_update_preserves_clock(self, service):
        service.expect("PUT", "/test/clock", 400, actor=None, body={"now": "bad timestamp"})
        assert service.expect("GET", "/test/clock", 200, actor=None) == {"now": FIXED}

    def test_initial_statuses_recalculate_at_exact_deadline(self, service):
        ticket = service.create()
        service.triage(
            ticket["id"],
            {"decision": "actively_exploited", "aware_at": FIXED, "reason": "Confirmed"},
        )
        service.fix_clock("2026-10-06T09:00:00Z")
        assert service.get(ticket["id"])["obligations"]["early_warning"]["status"] == "pending"
        service.fix_clock("2026-10-06T09:00:01Z")
        result = service.get(ticket["id"])
        assert result["obligations"]["early_warning"]["status"] == "overdue"
        assert result["obligations"]["notification"]["status"] == "pending"
        assert result["obligations"]["final_report"]["status"] == "awaiting_fix"
