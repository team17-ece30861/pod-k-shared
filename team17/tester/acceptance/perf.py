"""AC-18 performance check (REQ-17): with 1,000 tickets stored, send 200 mixed requests
one at a time; the 95th percentile latency, measured by this client, must be at most 1 s.

    export CRA_BASE_URL=https://<deployment-base-url>
    export CRA_API_TOKEN=<token>      # never commit it or paste it into a prompt
    python -m acceptance.perf --reset --out perf.json

Run it from the repository root. It is not part of the pytest suite or the deployed
tester: it loads about 2,400 requests' worth of data and takes minutes.

Phases, in order:

1. Warm-up: ``--warmup`` untimed requests (health, clock, list), reported on their own
   as cold-start numbers. Cloud Run and Neon may both be asleep before this.
2. Reset (only with ``--reset``): ``POST /test/reset`` deletes every ticket on the
   deployment, for every client. Without it the deployment must already be empty.
3. Load: valid tickets through the public API only (submit, triage, record
   submission, close), in the fixed ``LOAD_PROFILE`` mix. Not timed. By default it
   loads ``--tickets`` (1,000, as AC-18 says), and the 10 measured POSTs end the run at
   1,010. ``--exact-limit`` loads 990 instead, so the store never exceeds 1,000. A setup
   or load request that gets no response is retried up to 3 times, after checking the
   service so nothing is applied twice; retries are reported.
4. Measured: ``--requests`` requests sent one after another in the fixed
   ``MEASURED_MIX``, shuffled by ``--seed``. Each request has one expected status; any
   other status, a connection error or a response that breaks the contract schema is a
   failure, however fast it was. Measured requests are never retried. Outside the
   timer, every list response is compared with the tickets the script expects from what
   it loaded and changed: the unfiltered list must hold exactly those tickets, and each
   filtered list exactly the ones matching its filter.

Only a run with 1,000 tickets, 200 requests and response validation on is labelled an
AC-18 result (PASS or FAIL). Any other run prints its numbers as "not an AC-18 run", and
``--no-validate`` prints "NOT AC-18 EVIDENCE: validation off".

Times are derived from the service's own clock (``GET /test/clock``), read once before
loading, and always lie in the past, so the script never sets the clock. A fixed clock
left by an earlier run is used as it is.

Latency is the wall time of one HTTP request, from sending it to reading and
decompressing the whole response body. The transport behaves like a normal HTTP client:
it keeps one keep-alive connection per thread, asks for gzip (``Accept-Encoding:
gzip``) and opens a new connection only when the server closed the old one. A
connection the server closed while it sat idle is noticed before the next request is
written and reopened without counting as a retry; a connection that drops after a
request was sent is a failure in the measured phase, as before. Connections opened are
counted and recorded. ``--no-keepalive`` restores the earlier method: a new TCP (and
TLS) connection per request and no compression, as the acceptance runner does.

Percentiles use the nearest-rank method: p95 of 200 samples is the 190th fastest.

Exit status: 0 if p95 <= 1 s and nothing failed, 1 if not, 2 if the run could not
start or the load failed. Afterwards the deployment holds the loaded tickets; a final
``POST /test/reset`` is the operator's call.
"""

import argparse
import gzip
import http.client
import json
import math
import os
import random
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from acceptance.client import ROOT, Contract, HTTPTransport, Response

ACTOR = "perf-ac18"
P95_LIMIT_SECONDS = 1.0
DEFAULT_SEED = 17

# Measured operations and their share of the 200 requests (sum 200). Each name maps
# to one contract operation; the three list variants are reported separately.
MEASURED_MIX = {
    "list": 20,  # GET /tickets
    "list_state": 10,  # GET /tickets?state=...
    "list_status": 10,  # GET /tickets?obligation_status=...
    "get": 50,  # GET /tickets/{id}
    "draft": 30,  # GET /tickets/{id}/reports/{type}
    "audit": 20,  # GET /tickets/{id}/audit
    "patch": 20,  # PATCH /tickets/{id}
    "create": 10,  # POST /tickets
    "triage": 10,  # POST /tickets/{id}/triage
    "submission": 10,  # POST /tickets/{id}/reports/{type}/submission
    "close": 10,  # POST /tickets/{id}/close
}

OPERATIONS = {
    "list": ("GET", "/tickets", 200),
    "list_state": ("GET", "/tickets", 200),
    "list_status": ("GET", "/tickets", 200),
    "get": ("GET", "/tickets/{id}", 200),
    "draft": ("GET", "/tickets/{id}/reports/{type}", 200),
    "audit": ("GET", "/tickets/{id}/audit", 200),
    "patch": ("PATCH", "/tickets/{id}", 200),
    "create": ("POST", "/tickets", 201),
    "triage": ("POST", "/tickets/{id}/triage", 200),
    "submission": ("POST", "/tickets/{id}/reports/{type}/submission", 200),
    "close": ("POST", "/tickets/{id}/close", 200),
}

# What the loaded tickets look like, as shares of --tickets (sum 1).
LOAD_PROFILE = {
    "received_min": Fraction(15, 100),  # required fields only, never triaged
    "received_full": Fraction(15, 100),  # every field, never triaged
    "reportable": Fraction(25, 100),  # triaged actively_exploited, nothing recorded
    "early_warning": Fraction(10, 100),  # + early warning recorded
    "notification": Fraction(10, 100),  # + early warning and notification recorded
    "final_report": Fraction(5, 100),  # all three recorded, still reportable
    "resolved": Fraction(5, 100),  # all three recorded, then closed (resolved)
    "not_reportable": Fraction(15, 100),  # triaged not_actively_exploited or invalid
}
REPORTABLE = ("reportable", "early_warning", "notification", "final_report", "resolved")
RECORDED = {
    "reportable": (),
    "early_warning": ("early_warning",),
    "notification": ("early_warning", "notification"),
    "final_report": ("early_warning", "notification", "final_report"),
    "resolved": ("early_warning", "notification", "final_report"),
}

# Pools the mutating measured operations draw from, without replacement and disjoint,
# so every measured request has a known expected status.
MUTATION_POOLS = {
    "triage": ("received_full",),
    "submission": ("reportable", "early_warning"),
    "close": ("final_report",),
    "patch": ("received_min", "reportable", "early_warning", "notification"),
}
READ_POOLS = {
    "get": tuple(LOAD_PROFILE),
    "audit": tuple(LOAD_PROFILE),
    "draft": REPORTABLE,
}
LIST_STATES = ("received", "reportable", "closed")
LIST_STATUSES = ("pending", "overdue", "submitted", "awaiting_fix", "not_applicable")
DRAFT_TYPES = ("early_warning", "notification", "final_report", "user_advisory")
MEMBER_STATES = ("DE", "NL", "FR", "IE", "AT", "BE", "ES", "IT", "PL", "SE")


class SetupError(Exception):
    """The run cannot start or its load failed; nothing was measured."""


# ---------------------------------------------------------------- pure helpers


def percentile(values, pct) -> float:
    """Nearest-rank percentile: the smallest value with at least pct% of values at or
    below it. percentile(200 samples, 95) is the 190th smallest."""
    if not values:
        raise ValueError("no values")
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    ordered = sorted(values)
    rank = math.ceil(Fraction(str(pct)) * len(ordered) / 100)
    return ordered[rank - 1]


def apportion(shares: dict, total: int) -> dict:
    """Split total into integer counts proportional to shares (largest remainder, ties
    to the earlier key), so the counts always sum to total."""
    weight = sum(Fraction(value) for value in shares.values())
    exact = {key: Fraction(value) * total / weight for key, value in shares.items()}
    counts = {key: math.floor(value) for key, value in exact.items()}
    order = sorted(exact, key=lambda key: (-(exact[key] - counts[key]), list(shares).index(key)))
    for key in order[: total - sum(counts.values())]:
        counts[key] += 1
    return counts


def summarize(seconds) -> dict:
    return {
        "n": len(seconds),
        "p50": percentile(seconds, 50),
        "p95": percentile(seconds, 95),
        "p99": percentile(seconds, 99),
        "max": max(seconds),
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _full_fields(rng: random.Random, number: int, base: datetime, aware: datetime | None):
    """Every client field, so every draft is complete. The fix time is in the past and,
    for a reportable ticket, usually after awareness (sometimes before, as in E5)."""
    if aware is None:
        fix_at = base - timedelta(hours=rng.randint(1, 240))
    elif rng.random() < 0.8:
        fix_at = aware + (base - aware) * rng.random()
    else:
        fix_at = aware - timedelta(hours=rng.randint(1, 72))
    return {
        "member_states": rng.sample(MEMBER_STATES, rng.randint(1, 3)),
        "vulnerability": {
            "cve_id": f"CVE-2026-{10000 + number}",
            "description": "Crafted session cookie bypasses authentication. " * 4,
        },
        "exploit": {"description": "Observed against exposed admin interfaces."},
        "mitigations": {
            "taken": "Rotated session keys and blocked the attack pattern.",
            "for_users": "Restrict the admin interface to trusted networks.",
        },
        "impact": "An attacker can take over the device remotely.",
        "malicious_actor": "Unknown",
        "sensitivity": rng.choice(["CLEAR", "GREEN", "AMBER", "AMBER+STRICT", "RED"]),
        "severity": rng.choice(["low", "medium", "high", "critical"]),
        "fix": {
            "available_at": _iso(fix_at),
            "description": "Session validation fixed.",
            "fixed_versions": [f"4.{number % 7}.{number % 5 + 1}"],
        },
    }


def ticket_body(rng: random.Random, number: int, base: datetime, *, full: bool, aware=None):
    body = {
        "source": rng.choice(["external_researcher", "customer", "internal", "scanner", "csirt"]),
        "title": f"Perf ticket {number}: authentication bypass",
        "description": "Reporter describes a login bypass using a crafted session cookie. "
        * rng.randint(1, 8),
        "product": {
            "manufacturer": "Acme",
            "name": f"Router OS {number % 12}",
            "affected_versions": [f"4.{number % 7}.0", f"4.{number % 7}.1"],
        },
        "reporter": {"name": f"Reporter {number}", "contact": f"r{number}@example.invalid"},
    }
    if full:
        body.update(_full_fields(rng, number, base, aware))
    return body


def load_size(tickets: int, mix: dict, *, exact_limit: bool = False) -> int:
    """How many tickets to load: `tickets` (AC-18's wording), or with exact_limit few
    enough that the store holds `tickets` after the measured POSTs."""
    count = tickets - mix.get("create", 0) if exact_limit else tickets
    if count < 1:
        raise ValueError("--tickets must be larger than the measured POSTs")
    return count


def build_load_specs(seed: int, tickets: int, base: datetime) -> list[dict]:
    """The loaded tickets, in creation order: a pure function of seed, count and the
    service time read at the start. Each spec is the request chain for one ticket."""
    rng = random.Random(f"load-{seed}")
    categories = [
        category
        for category, count in apportion(LOAD_PROFILE, tickets).items()
        for _ in range(count)
    ]
    specs = []
    for number, category in enumerate(categories):
        spec = {"category": category, "triage": None, "submissions": [], "close": False}
        aware = None
        if category in REPORTABLE:
            aware = base - timedelta(hours=rng.randint(2, 20 * 24))
            spec["triage"] = {
                "decision": "actively_exploited",
                "aware_at": _iso(aware),
                "reason": "Customer logs show exploitation.",
            }
        elif category == "not_reportable":
            spec["triage"] = {
                "decision": rng.choice(["not_actively_exploited", "invalid"]),
                "reason": "No sign of exploitation.",
            }
        full = category != "received_min"
        spec["body"] = ticket_body(rng, number, base, full=full, aware=aware)
        if category == "reportable" and rng.random() < 0.3:
            spec["body"].pop("fix")  # final report awaiting a fix
        recorded_at = aware
        for report in RECORDED.get(category, ()):
            # Somewhere between the last recorded report (or awareness) and now, so
            # some reports are on time and some late.
            recorded_at = recorded_at + (base - recorded_at) * rng.random() * 0.5
            spec["submissions"].append(
                {"type": report, "body": {"submitted_at": _iso(recorded_at)}}
            )
        spec["close"] = category == "resolved"
        specs.append(spec)
    return specs


def build_plan(seed: int, mix: dict, category_sizes: dict) -> list[dict]:
    """The measured requests, in order: a pure function of seed, mix and the number of
    loaded tickets in each category. Targets are (category, index-within-category);
    server IDs are filled in at run time. Raises ValueError if a pool is too small."""
    rng = random.Random(f"plan-{seed}")
    available = {category: list(range(size)) for category, size in sorted(category_sizes.items())}
    for indexes in available.values():
        rng.shuffle(indexes)
    reserved = set()

    def take(op):
        for category in MUTATION_POOLS[op]:
            if available.get(category):
                target = (category, available[category].pop())
                reserved.add(target)
                return target
        raise ValueError(f"not enough loaded tickets for {mix[op]} '{op}' requests")

    def pick(op):
        categories = [c for c in READ_POOLS[op] if category_sizes.get(c)]
        if not categories:
            raise ValueError(f"no loaded tickets for '{op}' requests")
        weights = [category_sizes[c] for c in categories]
        category = rng.choices(categories, weights)[0]
        return (category, rng.randrange(category_sizes[category]))

    ops = [op for op, count in mix.items() for _ in range(count)]
    rng.shuffle(ops)
    plan = []
    for position, op in enumerate(ops):
        step = {"op": op}
        if op == "list_state":
            step["query"] = {"state": rng.choice(LIST_STATES)}
        elif op == "list_status":
            step["query"] = {"obligation_status": rng.choice(LIST_STATUSES)}
        elif op in READ_POOLS:
            step["target"] = pick(op)
            if op == "draft":
                step["type"] = rng.choice(DRAFT_TYPES)
        elif op in MUTATION_POOLS:
            step["target"] = take(op)
            if op == "submission":
                category = step["target"][0]
                step["type"] = "early_warning" if category == "reportable" else "notification"
            elif op == "triage":
                step["decision"] = rng.choice(
                    ["actively_exploited", "actively_exploited", "not_actively_exploited"]
                )
                step["aware_hours_ago"] = rng.randint(1, 96)
            elif op == "patch":
                step["severity"] = rng.choice(["low", "medium", "high", "critical"])
        elif op == "create":
            step["full"] = rng.random() < 0.5
        step["number"] = position
        plan.append(step)
    return plan


# ---------------------------------------------------------------- HTTP

CONNECTION_KEEPALIVE = "keep-alive, gzip requested (Accept-Encoding: gzip)"
CONNECTION_PER_REQUEST = "new TCP (and TLS for https) connection per request"


class KeepAliveTransport(HTTPTransport):
    """HTTPTransport's checks and send() signature, sent over one persistent connection
    per thread (http.client connections aren't thread-safe; the load runs in a few
    threads) with Accept-Encoding: gzip. Never resends a request that was written."""

    def __init__(self, base_url: str, token: str, timeout: float = 30):
        super().__init__(base_url, token)
        parsed = urlsplit(self.base_url)
        self._https = parsed.scheme == "https"
        self._host, self._port = parsed.hostname, parsed.port
        self._prefix = parsed.path.rstrip("/")
        self._timeout = timeout
        self._local = threading.local()
        self._count_lock = threading.Lock()
        self.connections_opened = 0
        # What actually arrived, as opposed to what was asked for.
        self.responses_received = 0
        self.gzip_responses = 0

    def send(
        self,
        method,
        path,
        *,
        body=None,
        raw=None,
        actor="phase3-tests",
        authenticated=True,
        token=None,
    ):
        if body is not None and raw is not None:
            raise ValueError("provide JSON body or raw bytes, not both")
        data = json.dumps(body).encode("utf-8") if body is not None else raw
        headers = {"Content-Type": "application/json", "Accept-Encoding": "gzip"}
        if actor is not None:
            headers["X-Actor"] = actor
        if authenticated:
            headers["Authorization"] = "Bearer " + (token if token is not None else self.token)
        connection = self._connection()
        try:
            connection.request(method, self._prefix + path, body=data, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except Exception:
            self._discard()
            raise
        if response.will_close:
            self._discard()
        gzipped = response.getheader("Content-Encoding", "").strip().lower() == "gzip"
        if gzipped:
            payload = gzip.decompress(payload)
        with self._count_lock:
            self.responses_received += 1
            self.gzip_responses += gzipped
        return Response(response.status, response.getheader("Content-Type", ""), payload)

    def _connection(self) -> http.client.HTTPConnection:
        """This thread's open connection, reopened first if the server closed it while
        it was idle (nothing has been sent on it yet, so this is not a retry)."""
        connection = getattr(self._local, "connection", None)
        if connection is not None and (connection.sock is None or _closed_by_peer(connection)):
            self._discard()
            connection = None
        if connection is None:
            cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            connection = cls(self._host, self._port, timeout=self._timeout)
            connection.connect()
            with self._count_lock:
                self.connections_opened += 1
            self._local.connection = connection
        return connection

    def _discard(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
        self._local.connection = None


def _closed_by_peer(connection) -> bool:
    """An idle keep-alive socket is readable only if the server closed it (or sent
    something unasked), either way it can't carry the next request."""
    readable, _, _ = select.select([connection.sock], [], [], 0)
    return bool(readable)


def _timed(transport, method, path, body=None, actor=ACTOR):
    started = time.perf_counter()
    try:
        response = transport.send(method, path, body=body, actor=actor)
    except Exception as error:  # timeout, refused, reset: a failure, not a crash
        return None, time.perf_counter() - started, f"{type(error).__name__}"
    return response, time.perf_counter() - started, None


def _ticket_path(ticket_id: str, suffix: str = "") -> str:
    return "/tickets/" + quote(ticket_id, safe="") + suffix


# Setup and load only (never the measured phase): a request that got no response at
# all (dropped connection, timeout) is tried again, up to 3 more times. A request that
# changes data is first checked against the service, so a retry never applies a change
# twice or creates a duplicate ticket.
LOAD_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


class Loader:
    """Setup and load requests with safe retries; records every retry."""

    def __init__(self, transport, sleep=time.sleep):
        self.transport = transport
        self.sleep = sleep
        self.retries = []
        self._lock = threading.Lock()

    def call(
        self,
        method,
        path,
        expected,
        body=None,
        actor=ACTOR,
        *,
        label=None,
        find_existing=None,
        confirm_done=None,
    ):
        """find_existing(): after a lost response, before resending, returns the result
        if the first attempt did take effect (else None). confirm_done(): after a lost
        response, if the resend gets 409, returns True if the change already happened."""
        label = label or path.split("?")[0]
        lost = False
        for attempt in range(len(LOAD_RETRY_BACKOFF_SECONDS) + 1):
            if lost and find_existing is not None:
                found = find_existing()
                if found is not None:
                    self._record(method, label, "found the first attempt's result")
                    return found
            response, _, error = _timed(self.transport, method, path, body, actor)
            if response is None:
                if attempt == len(LOAD_RETRY_BACKOFF_SECONDS):
                    raise SetupError(
                        f"{method} {label}: no response after {attempt + 1} attempts ({error})"
                    )
                self._record(method, label, f"no response ({error}), retrying")
                self.sleep(LOAD_RETRY_BACKOFF_SECONDS[attempt])
                lost = True
                continue
            if response.status == expected:
                return json.loads(response.body) if response.body else None
            if lost and response.status == 409 and confirm_done is not None and confirm_done():
                self._record(method, label, "409 on retry; confirmed already done")
                return None
            raise SetupError(f"{method} {label}: expected {expected}, got {response.status}")

    def _record(self, method, label, outcome):
        with self._lock:
            self.retries.append({"method": method, "path": label, "outcome": outcome})

    def get(self, path):
        return self.call("GET", path, 200, actor=None)

    def create(self, body) -> dict:
        def find_existing():
            # The deployment was empty before the load and load titles are unique.
            matches = [t for t in self.get("/tickets") if t["title"] == body["title"]]
            if len(matches) > 1:
                raise SetupError(f"duplicate tickets titled {body['title']!r}")
            return matches[0] if matches else None

        return self.call(
            "POST", "/tickets", 201, body=body, label="/tickets", find_existing=find_existing
        )

    def change(self, ticket_id, suffix, label, body, done):
        """A triage, submission or close; done(ticket) says whether it already happened."""
        self.call(
            "POST",
            _ticket_path(ticket_id, suffix),
            200,
            body=body,
            label=label,
            confirm_done=lambda: done(self.get(_ticket_path(ticket_id))),
        )


def load_ticket(loader: Loader, spec) -> str:
    ticket_id = loader.create(spec["body"])["id"]
    if spec["triage"]:
        expected = "reportable" if spec["triage"]["decision"] == "actively_exploited" else "closed"
        loader.change(
            ticket_id,
            "/triage",
            "/tickets/{id}/triage",
            spec["triage"],
            lambda ticket, expected=expected: ticket["state"] == expected,
        )
    for submission in spec["submissions"]:
        report = submission["type"]
        loader.change(
            ticket_id,
            f"/reports/{report}/submission",
            "/tickets/{id}/reports/{type}/submission",
            submission["body"],
            lambda ticket, report=report: ticket["obligations"][report]["submitted_at"] is not None,
        )
    if spec["close"]:
        loader.change(
            ticket_id,
            "/close",
            "/tickets/{id}/close",
            None,
            lambda ticket: ticket["state"] == "closed" and ticket["resolution"] == "resolved",
        )
    return ticket_id


def load_all(loader: Loader, specs, workers: int) -> list[str]:
    """Loads every spec, a few at a time; stops at the first failure."""
    pool = ThreadPoolExecutor(workers)
    futures = [pool.submit(load_ticket, loader, spec) for spec in specs]
    try:
        return [future.result() for future in futures]
    finally:
        pool.shutdown(cancel_futures=True)


def run_warmup(transport, count: int) -> list[dict]:
    cycle = [("GET", "/health"), ("GET", "/test/clock"), ("GET", "/tickets")]
    results = []
    for index in range(count):
        method, path = cycle[index % len(cycle)]
        response, seconds, error = _timed(transport, method, path, actor=None)
        results.append(
            {
                "method": method,
                "path": path,
                "status": response.status if response else None,
                "error": error,
                "seconds": seconds,
            }
        )
    return results


def _request_for(step, ids, base, created):
    op = step["op"]
    method, template, expected = OPERATIONS[op]
    body = None
    ticket_id = ids[step["target"]] if "target" in step else None
    if op in ("list", "list_state", "list_status"):
        path = "/tickets" + ("?" + urlencode(step["query"]) if "query" in step else "")
    elif op == "get":
        path = _ticket_path(ticket_id)
    elif op == "patch":
        path = _ticket_path(ticket_id)
        body = {"title": f"Perf update {step['number']}", "severity": step["severity"]}
    elif op == "audit":
        path = _ticket_path(ticket_id, "/audit")
    elif op == "draft":
        path = _ticket_path(ticket_id, f"/reports/{step['type']}")
    elif op == "submission":
        path = _ticket_path(ticket_id, f"/reports/{step['type']}/submission")
        body = {"reference": f"PERF-{step['number']}"}
    elif op == "close":
        path = _ticket_path(ticket_id, "/close")
    elif op == "triage":
        path = _ticket_path(ticket_id, "/triage")
        body = {"decision": step["decision"], "reason": "Measured triage."}
        if step["decision"] == "actively_exploited":
            body["aware_at"] = _iso(base - timedelta(hours=step["aware_hours_ago"]))
    else:  # create
        path = "/tickets"
        rng = random.Random(f"create-{step['number']}")
        body = ticket_body(rng, 100000 + created, base, full=step["full"])
    return method, template, path, expected, body


# ---------------------------------------------------------------- expected lists
#
# The script's own model of every stored ticket (state, awareness, fix time, recorded
# reports), built from what it sent, so list responses can be checked independently.
# Statuses follow docs/spec.md Model B: early warning due 24 h after awareness,
# notification 72 h, final report 14 days after the later of fix and awareness.


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def model_entry(body: dict) -> dict:
    fix = (body.get("fix") or {}).get("available_at")
    return {
        "state": "received",
        "resolution": None,
        "aware": None,
        "fix": _parse(fix) if fix else None,
        "subs": {},
    }


def apply_triage(entry: dict, triage: dict) -> None:
    if triage["decision"] == "actively_exploited":
        entry.update(state="reportable", aware=_parse(triage["aware_at"]))
    else:
        entry.update(state="closed", resolution=triage["decision"])


def model_from_load(specs, loaded) -> dict:
    model = {}
    for spec, ticket_id in zip(specs, loaded, strict=True):
        entry = model[ticket_id] = model_entry(spec["body"])
        if spec["triage"]:
            apply_triage(entry, spec["triage"])
        for submission in spec["submissions"]:
            entry["subs"][submission["type"]] = _parse(submission["body"]["submitted_at"])
        if spec["close"]:
            entry.update(state="closed", resolution="resolved")
    return model


def obligation_statuses(entry: dict, now: datetime) -> set[str]:
    """The statuses of a ticket's three reports at `now`."""
    aware = entry["aware"]
    if aware is None or entry["resolution"] in ("invalid", "not_actively_exploited"):
        return {"not_applicable"}
    fix = entry["fix"]
    dues = {
        "early_warning": aware + timedelta(hours=24),
        "notification": aware + timedelta(hours=72),
        "final_report": None if fix is None else max(fix, aware) + timedelta(days=14),
    }
    statuses = set()
    for report_type, due in dues.items():
        submitted = entry["subs"].get(report_type)
        if submitted is not None:
            statuses.add("submitted" if submitted <= due else "submitted_late")
        elif due is None:
            statuses.add("awaiting_fix")
        else:
            statuses.add("pending" if now <= due else "overdue")
    return statuses


def expected_ids(model: dict, query: dict, earliest: datetime, latest: datetime):
    """(certain, possible) ticket IDs a list with this filter must return when the
    service clock is somewhere in [earliest, latest]. Each "any report has status X"
    test changes at most once in that window, so checking both ends is enough."""
    certain, possible = set(), set()
    for ticket_id, entry in model.items():
        if "state" in query:
            outcomes = {entry["state"] == query["state"]}
        elif "obligation_status" in query:
            wanted = query["obligation_status"]
            outcomes = {wanted in obligation_statuses(entry, t) for t in (earliest, latest)}
        else:
            outcomes = {True}
        if True in outcomes:
            possible.add(ticket_id)
            if outcomes == {True}:
                certain.add(ticket_id)
    return certain, possible


def check_list(returned, model, query, earliest, latest):
    """None if the list holds exactly the expected tickets, else what is wrong."""
    ids = [ticket["id"] for ticket in returned]
    certain, possible = expected_ids(model, query, earliest, latest)
    missing, extra = certain - set(ids), set(ids) - possible
    if len(ids) != len(set(ids)) or missing or extra:
        return (
            f"list {urlencode(query) or 'unfiltered'}: {len(ids)} tickets, expected "
            f"{len(certain)}-{len(possible)} ({len(missing)} missing, {len(extra)} extra)"
        )
    return None


def update_model(model, step, ids, body, parsed) -> None:
    """Applies a successful measured change to the model."""
    op = step["op"]
    if op == "create":
        model[parsed["id"]] = model_entry(body)
    elif op == "triage":
        apply_triage(model[ids[step["target"]]], body)
    elif op == "submission":
        model[ids[step["target"]]]["subs"][step["type"]] = _parse(parsed["submitted_at"])
    elif op == "close":
        model[ids[step["target"]]].update(state="closed", resolution="resolved")


def run_measured(
    transport, plan, ids, base, model=None, contract=None, clock_read_at=None
) -> list[dict]:
    """Sends the plan one request at a time. With a model, list responses are checked
    against it and the model follows each successful change."""
    results = []
    created = 0
    clock_read_at = time.perf_counter() if clock_read_at is None else clock_read_at
    for step in plan:
        method, template, path, expected, body = _request_for(step, ids, base, created)
        response, seconds, error = _timed(transport, method, path, body)
        # Everything below is outside the timer.
        status = response.status if response else None
        problem = error
        if status is not None and status != expected:
            problem = f"expected {expected}, got {status}"
        parsed = None
        if problem is None:
            try:
                if contract is not None:
                    parsed = contract.validate(method, template, response)
                else:
                    parsed = json.loads(response.body) if response.body else None
            except (AssertionError, ValueError) as failure:
                problem = f"schema: {str(failure)[:200]}"
        if problem is None and step["op"] == "create":
            created += 1
        if problem is None and model is not None:
            if step["op"] in ("list", "list_state", "list_status"):
                # The service clock is base plus at most the time since it was read
                # (plus a second for its truncation).
                elapsed = time.perf_counter() - clock_read_at + 1
                latest = base + timedelta(seconds=math.ceil(elapsed))
                problem = check_list(parsed, model, step.get("query", {}), base, latest)
            else:
                update_model(model, step, ids, body, parsed)
        results.append(
            {
                "op": step["op"],
                "method": method,
                "path": template,
                "expected_status": expected,
                "status": status,
                "seconds": seconds,
                "ok": problem is None,
                "problem": problem,
            }
        )
    return results


def report(results, warmup, limit=P95_LIMIT_SECONDS) -> dict:
    seconds = [result["seconds"] for result in results]
    by_op = {}
    for result in results:
        by_op.setdefault(result["op"], []).append(result["seconds"])
    failures = [result for result in results if not result["ok"]]
    overall = summarize(seconds)
    warm = [entry["seconds"] for entry in warmup]
    return {
        "overall": overall,
        "per_operation": {
            op: {"method": OPERATIONS[op][0], "path": OPERATIONS[op][1], **summarize(values)}
            for op, values in by_op.items()
        },
        "unexpected": len(failures),
        "failures": [
            {key: f[key] for key in ("op", "method", "path", "status", "problem")} for f in failures
        ],
        "warmup": {
            "requests": warmup,
            "first_seconds": warm[0] if warm else None,
            "max_seconds": max(warm) if warm else None,
            "unexpected": sum(entry["status"] != 200 for entry in warmup),
        },
        "p95_limit_seconds": limit,
        "passed": overall["p95"] <= limit and not failures,
    }


# ---------------------------------------------------------------- CLI


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _now_utc() -> str:
    return datetime.fromtimestamp(time.time(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(value) -> str:
    return f"{value * 1000:8.1f} ms"


def verdict(passed: bool, *, validated: bool, tickets: int, requests: int) -> str:
    """PASS or FAIL only for a real AC-18 run: 1,000 tickets, 200 requests, validated."""
    if not validated:
        return "NOT AC-18 EVIDENCE: validation off"
    if tickets != 1000 or requests != 200:
        return "not an AC-18 run"
    return "PASS" if passed else "FAIL"


def _print_summary(result):
    label = "AC-18" if result["ac18_run"] else "Performance run (not AC-18)"
    print(
        f"\n{label} against {result['base_url']} ({result['ticket_count_at_start']} tickets "
        f"at the start, {result['ticket_count_at_end']} at the end; "
        f"validated: {str(result['validated']).lower()})"
    )
    opened = result.get("connections_opened") or {}
    if opened.get("total") is None:
        print(f"Connection: {result['connection']}")
    else:
        print(
            f"Connection: {result['connection']} ({opened['total']} opened in all, "
            f"{opened['measured']} during the measured requests)"
        )
    gzipped = result.get("gzip_responses") or {}
    if gzipped.get("measured") is not None:
        print(
            f"Gzip-encoded responses: {gzipped['measured']} of "
            f"{gzipped['measured_received']} measured responses received"
        )
    warm = result["summary"]["warmup"]
    if warm["requests"]:
        print(
            f"Cold start (untimed warm-up, n={len(warm['requests'])}): first "
            f"{_ms(warm['first_seconds'])}, max {_ms(warm['max_seconds'])}, "
            f"unexpected statuses {warm['unexpected']}"
        )
    overall = result["summary"]["overall"]
    print(
        f"Measured n={overall['n']}: p50 {_ms(overall['p50'])}  p95 {_ms(overall['p95'])}  "
        f"p99 {_ms(overall['p99'])}  max {_ms(overall['max'])}"
    )
    print(f"{'operation':<14}{'request':<44}{'n':>4}{'p50':>12}{'p95':>12}{'max':>12}")
    for op, row in result["summary"]["per_operation"].items():
        request = f"{row['method']} {row['path']}"
        print(
            f"{op:<14}{request:<44}{row['n']:>4}"
            f"{_ms(row['p50']):>12}{_ms(row['p95']):>12}{_ms(row['max']):>12}"
        )
    print(f"Unexpected statuses or responses: {result['summary']['unexpected']}")
    for failure in result["summary"]["failures"][:20]:
        print(f"  {failure['method']} {failure['path']}: {failure['problem']}")
    within = "within" if result["summary"]["passed"] else "NOT within"
    print(
        f"{result['verdict']}: p95 {_ms(overall['p95']).strip()} "
        f"({within} the {P95_LIMIT_SECONDS:.0f} s limit, "
        f"{result['summary']['unexpected']} unexpected)"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m acceptance.perf", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--base-url", default=os.environ.get("CRA_BASE_URL"))
    parser.add_argument(
        "--reset",
        action="store_true",
        help="POST /test/reset first: deletes ALL data on the deployment, for every client",
    )
    parser.add_argument("--tickets", type=int, default=1000)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument(
        "--exact-limit",
        action="store_true",
        help="load --tickets minus the measured POSTs (990), so the store ends at exactly "
        "--tickets; by default the full --tickets are loaded and the POSTs end at 1,010",
    )
    parser.add_argument("--warmup", type=int, default=5, help="untimed cold-start requests")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--load-workers", type=int, default=4, help="concurrent loaders")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="skip contract schema checks of responses (the run is then not AC-18 evidence)",
    )
    parser.add_argument(
        "--tested-version", default=os.environ.get("CRA_TESTED_VERSION", "unspecified")
    )
    parser.add_argument(
        "--no-keepalive",
        action="store_true",
        help="the earlier method: a new connection per request and no gzip",
    )
    parser.add_argument("--out", help="write the JSON result here")
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("provide --base-url or CRA_BASE_URL")
    if not os.environ.get("CRA_API_TOKEN"):
        parser.error("set CRA_API_TOKEN in the environment")
    if args.tickets < 1 or args.requests < 1 or args.warmup < 0 or args.load_workers < 1:
        parser.error("--tickets, --requests and --load-workers must be positive")
    return args


def run(args) -> int:
    token = os.environ["CRA_API_TOKEN"]
    if args.no_keepalive:
        transport = HTTPTransport(args.base_url, token)
    else:
        transport = KeepAliveTransport(args.base_url, token)
    mix = apportion(MEASURED_MIX, args.requests)
    load_count = load_size(args.tickets, mix, exact_limit=args.exact_limit)
    validated = not args.no_validate
    result = {
        "criterion": "AC-18 / REQ-17: p95 <= 1 s over sequential mixed requests",
        "base_url": transport.base_url,
        "tested_version_label": args.tested_version,
        "script_commit": _commit(),
        "started_at": _now_utc(),
        "seed": args.seed,
        "tickets": args.tickets,
        "loaded": load_count,
        "exact_limit": args.exact_limit,
        "validated": validated,
        "ac18_run": validated and args.tickets == 1000 and args.requests == 200,
        "requests": args.requests,
        "mix": mix,
        "percentile_method": "nearest rank",
        "connection": CONNECTION_PER_REQUEST if args.no_keepalive else CONNECTION_KEEPALIVE,
        "reset": args.reset,
    }
    loader = None
    try:
        plan = build_plan(args.seed, mix, apportion(LOAD_PROFILE, load_count))
        print(f"Target: {transport.base_url}")
        print(f"Warm-up: {args.warmup} untimed requests")
        warmup = run_warmup(transport, args.warmup)
        loader = Loader(transport)
        if args.reset:
            print("Resetting the WHOLE deployment (POST /test/reset): every client's data")
            loader.call("POST", "/test/reset", 204, actor=None)  # idempotent
        existing = loader.get("/tickets")
        if existing:
            raise SetupError(
                f"the deployment already holds {len(existing)} tickets; use an empty "
                "deployment or pass --reset (deletes everything)"
            )
        clock = loader.get("/test/clock")["now"]
        clock_read_at = time.perf_counter()
        base = datetime.fromisoformat(clock.replace("Z", "+00:00"))
        result["service_clock_at_start"] = clock
        specs = build_load_specs(args.seed, load_count, base)
        print(f"Loading {load_count} tickets ({args.load_workers} at a time, not timed)...")
        load_started = time.perf_counter()
        loaded = load_all(loader, specs, args.load_workers)
        result["load_seconds"] = round(time.perf_counter() - load_started, 1)
        ids, seen = {}, {}
        for spec, ticket_id in zip(specs, loaded, strict=True):
            index = seen.get(spec["category"], 0)
            ids[(spec["category"], index)] = ticket_id
            seen[spec["category"]] = index + 1
        stored = len(loader.get("/tickets"))
        if stored != len(specs):
            raise SetupError(f"loaded {len(specs)} tickets but {stored} are stored")
        result["ticket_count_at_start"] = stored
        result["ticket_count_at_end"] = stored + mix.get("create", 0)
        print(f"Loaded in {result['load_seconds']} s; {stored} tickets stored.")
        print(f"Measuring {args.requests} requests, one at a time...")
        contract = Contract() if validated else None
        model = model_from_load(specs, loaded)
        opened_before = _opened(transport)
        received_before = _counter(transport, "responses_received")
        gzip_before = _counter(transport, "gzip_responses")
        measured = run_measured(transport, plan, ids, base, model, contract, clock_read_at)
        result["connections_opened"] = {
            "total": _opened(transport),
            "measured": _opened(transport) - opened_before if opened_before is not None else None,
        }
        result["gzip_responses"] = {
            "measured": _counter(transport, "gzip_responses") - gzip_before
            if gzip_before is not None
            else None,
            "measured_received": _counter(transport, "responses_received") - received_before
            if received_before is not None
            else None,
        }
    except SetupError as error:
        result["error"] = str(error)
        result["connections_opened"] = {"total": _opened(transport), "measured": None}
        _note_retries(result, loader)
        print(f"Not measured: {error}", file=sys.stderr)
        _write(args.out, result)
        return 2
    result["finished_at"] = _now_utc()
    _note_retries(result, loader)
    result["summary"] = report(measured, warmup)
    result["verdict"] = verdict(
        result["summary"]["passed"],
        validated=validated,
        tickets=args.tickets,
        requests=args.requests,
    )
    result["results"] = measured
    _write(args.out, result)
    _print_summary(result)
    print(
        "The deployment still holds the loaded tickets. A final POST /test/reset is the "
        "operator's call. The script did not set the test clock."
    )
    return 0 if result["summary"]["passed"] else 1


def _counter(transport, name):
    """A KeepAliveTransport counter; None for the one-connection-per-request transport."""
    return getattr(transport, name, None)


def _opened(transport):
    """Connections the transport opened so far; None for one connection per request."""
    return getattr(transport, "connections_opened", None)


def _note_retries(result, loader):
    """Setup and load retries (the measured phase never retries)."""
    retries = loader.retries if loader else []
    result["load_retries"] = {"count": len(retries), "retries": retries}
    if retries:
        print(f"Setup/load retries after lost responses: {len(retries)}", file=sys.stderr)
        for retry in retries[:20]:
            print(f"  {retry['method']} {retry['path']}: {retry['outcome']}", file=sys.stderr)


def _write(destination, result):
    if destination:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except ValueError as error:  # a bad base URL or a pool too small for the mix
        print(f"Not measured: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
