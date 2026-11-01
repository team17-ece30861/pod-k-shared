from fastapi.testclient import TestClient
import httpx

import tester.main as tester_main


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def test_tester_setup_run_and_report(monkeypatch):
    def fake_request(method, target_url, path, *, json=None, actor=False):
        if method == "POST" and path == "/test/setup":
            return FakeResponse(201, {"session_id": json["session_id"]})
        if method == "GET" and path.startswith("/test/report?"):
            return FakeResponse(
                200,
                {
                    "coverage": {
                        "code": {
                            "covered": None,
                            "total": None,
                            "basis": "Source lines; instrumentation unavailable",
                        }
                    }
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(tester_main, "_request", fake_request)
    monkeypatch.setattr(
        tester_main,
        "_run_suite",
        lambda target: [
            {"test_id": "AC-01", "outcome": "pass", "message": "stubbed success"}
        ],
    )

    client = TestClient(tester_main.app)
    setup = client.post("/test/setup", json={"target_url": "https://example.test"})
    assert setup.status_code == 201
    session_id = setup.json()["session_id"]

    before = client.get("/test/report", params={"session_id": session_id})
    assert before.status_code == 409

    run = client.post("/test/run", json={"session_id": session_id})
    assert run.status_code == 200
    body = run.json()
    assert body["session_id"] == session_id
    assert body["target_url"] == "https://example.test"
    assert body["results"][0]["outcome"] == "pass"

    report = client.get("/test/report", params={"session_id": session_id})
    assert report.status_code == 200
    assert report.json() == body

    rerun = client.post("/test/run", json={"session_id": session_id})
    assert rerun.status_code == 409
