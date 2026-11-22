"""AC-13 (REQ-12): malformed requests generated from openapi.yaml.

Schemathesis generates negative cases for every ticket operation, deterministically
(fixed Hypothesis seed, derandomized, generate phase only). Each generated request is
then checked against the contract here: only requests that really break
`openapi.yaml` (body, `X-Actor`, a path `type`, or a declared query value) are kept, so
the run never sends a valid change by accident. Path IDs are replaced by seed tickets
in the state each operation needs, so a malformed request reaches validation instead
of stopping at 404.

The expected status follows the contract's precedence (401, 413, 404, 400, 409, 422):
an invalid path `type` is 404, a body over 64 KB is 413, anything else is 400. Every
response must be that status, documented for the operation, and a problem+json body
that matches the contract's error schema. Afterwards the stored data must be unchanged.
Failure reports retain status, media-type conformance and body size, but omit target
response bodies and exception messages because either may reflect credentials.

The same corpus runs in CI (`tests/test_malformed_requests.py`, Django test client) and
against a deployment:

    export CRA_BASE_URL=https://<deployment>   # or pass --base-url
    export CRA_API_TOKEN=<token>               # never printed
    python -m acceptance.malformed --out malformed.json [--reset]

Unlike the rest of `acceptance/`, this module needs Schemathesis (requirements-dev.txt).
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from urllib.request import Request

from jsonschema import Draft202012Validator, FormatChecker

from acceptance.client import Contract, HTTPTransport, Response, Service

SEED = 17
MAX_BODY_BYTES = 65536
PROBLEM = "application/problem+json"
# Schemathesis fills path IDs with this; it is replaced by a seed ticket at send time.
TICKET_PLACEHOLDER = "MALFORMEDSEEDTICKET"
# Generation needs some Authorization value so Schemathesis doesn't mutate the security
# scheme; the real token is added only when sending and never stored in a request.
AUTH_PLACEHOLDER = "Bearer generated-placeholder"

# Examples requested per operation. Byte-identical duplicates, requests the contract
# allows, and requests whose invalid input repeats one already kept (same operation and
# same invalid values, e.g. a missing X-Actor with a different valid body) are dropped, so
# each request sent has a different invalid input. Different invalid values for the same
# field and rule (type "+" and type "Ø") count separately; fault_categories() groups them.
# GET /tickets/{id} and GET /tickets/{id}/audit take only a free-form ID (any non-empty
# string is valid), so the contract leaves nothing in them to make malformed.
BUDGET = {
    "POST /tickets": 750,
    "GET /tickets": 300,
    "PATCH /tickets/{id}": 750,
    "POST /tickets/{id}/triage": 510,
    "GET /tickets/{id}/reports/{type}": 150,
    "POST /tickets/{id}/reports/{type}/submission": 510,
    "POST /tickets/{id}/close": 60,  # only 3 faults exist: X-Actor missing, empty, too long
}
MINIMUM_FAULTS = 500  # AC-13
# The corpus this repository expects (Python 3.12 and requirements-dev.txt's pins), per
# operating system: Linux and macOS generate different corpora even with the same Python
# patch release and identical package versions (cause inside Hypothesis/Schemathesis not
# identified). Both cover the same 133 fault categories; only the invalid values differ.
# CI checks only the Linux pin; the macOS pin is checked only on a Mac. Another Python or
# library version also generates different requests. If openapi.yaml or the generator
# changes on purpose, regenerate on both systems and update the values:
#   python -c "from acceptance.malformed import *; c = generate()[0]; print(len(c), fingerprint(c))"
PYTHON = (3, 12)
EXPECTED = {  # sys.platform: (requests, fingerprint)
    "linux": (664, "34824b069854e3e9"),
    "darwin": (683, "562ec91fbc181557"),
}


def expected_corpus() -> tuple[int, str] | None:
    """The recorded (requests, fingerprint) for this operating system, if there is one."""
    return EXPECTED.get(sys.platform)


UNGENERATED = {
    "GET /tickets/{id}": "only a free-form ticket ID; no input can break the contract",
    "GET /tickets/{id}/audit": "only a free-form ticket ID; no input can break the contract",
}
# The seed ticket each operation's ID is replaced with: a state where a valid request
# would succeed, so a malformed one that slipped through shows up as 2xx, not 409.
SEED_ROLE = {
    "PATCH /tickets/{id}": "received",
    "POST /tickets/{id}/triage": "received",
    "GET /tickets/{id}/reports/{type}": "reportable",
    "POST /tickets/{id}/reports/{type}/submission": "reportable",
    "POST /tickets/{id}/close": "reportable",
}
MUTATING = {"POST", "PATCH", "PUT", "DELETE"}
# Transport headers Schemathesis adds that aren't part of the case.
DROPPED_HEADERS = {
    "authorization",
    "user-agent",
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "x-schemathesis-testcaseid",
}
# What http.client will put on the wire (mirrors its private header checks), so the
# CLI and the in-process run send exactly the same corpus.
_LEGAL_HEADER_NAME = re.compile(rb"[^:\s][^:\r\n]*").fullmatch
_ILLEGAL_HEADER_VALUE = re.compile(rb"\n(?![ \t])|\r(?![ \t\n])").search


@dataclass(frozen=True)
class MalformedRequest:
    operation: str  # e.g. "POST /tickets/{id}/triage"
    method: str
    path: str  # path and query; a seed-ticket path holds TICKET_PLACEHOLDER
    headers: tuple[tuple[str, str], ...]  # never includes Authorization
    body: bytes | None
    violations: tuple[str, ...]  # what breaks the contract, e.g. "body: ...", "X-Actor: ..."
    fault: str  # the operation and the specific violations, ignoring the valid parts

    @property
    def expected_status(self) -> int:
        if self.body is not None and len(self.body) > MAX_BODY_BYTES:
            return 413
        if any(v.startswith("path ") for v in self.violations):
            return 404
        return 400

    def describe(self) -> dict:
        body = None
        if self.body is not None:
            body = self.body.decode("utf-8", errors="backslashreplace")
        return {
            "operation": self.operation,
            "method": self.method,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "violations": list(self.violations),
            "fault": self.fault,
            "expected_status": self.expected_status,
        }


@dataclass
class GenerationStats:
    generated: Counter = field(default_factory=Counter)
    duplicates: Counter = field(default_factory=Counter)  # byte-identical requests
    allowed_by_contract: Counter = field(default_factory=Counter)
    unsendable: Counter = field(default_factory=Counter)
    same_fault: Counter = field(default_factory=Counter)  # a new request for a fault already kept
    kept: Counter = field(default_factory=Counter)


GENERATION_FIELDS = [f.name for f in fields(GenerationStats)]


class RequestChecker:
    """Decides whether a concrete request breaks openapi.yaml, and how."""

    def __init__(self, contract: Contract):
        self.contract = contract
        self.resolver = contract.registry.resolver("urn:cra-contract")

    def _resolve(self, node):
        while isinstance(node, dict) and "$ref" in node:
            node = self.resolver.lookup(node["$ref"]).contents
        return node

    def _validator(self, schema):
        return Draft202012Validator(
            schema, registry=self.contract.registry, format_checker=FormatChecker()
        )

    def _parameters(self, path, method):
        item = self.contract.document["paths"][path]
        params = [*item.get("parameters", []), *item[method.lower()].get("parameters", [])]
        return [self._resolve(p) for p in params]

    def violations(self, operation, path_parameters, query, headers, body) -> list[tuple]:
        """(description, fault key) for each way the request breaks the contract. Fault keys
        name only the invalid part, so requests that differ only in valid parts match."""
        method, template = operation.split(" ", 1)
        found = []
        for param in self._parameters(template, method):
            name, location = param["name"], param["in"]
            schema = self._resolve(param["schema"])
            if location == "path":
                if name == "id":
                    continue  # replaced by a seed ticket
                value = unquote(path_parameters[name])
                if not self._validator(schema).is_valid(value):
                    found.append((f"path {name}: {value!r} is not allowed", ("path", name, value)))
            elif location == "query":
                for key, value in query:
                    if key == name and not self._validator(schema).is_valid(value):
                        found.append(
                            (f"query {name}: {value!r} is not allowed", ("query", name, value))
                        )
            elif location == "header":
                value = next((v for k, v in headers if k.lower() == name.lower()), None)
                if value is None:
                    if param.get("required"):
                        found.append((f"{name}: missing", ("header", name, "missing")))
                    continue
                # Surrounding whitespace is not part of an HTTP field value (RFC 9110 5.5).
                value = value.strip(" \t")
                if not self._validator(schema).is_valid(value):
                    kind = "empty" if not value else "too long" if len(value) > 200 else "invalid"
                    found.append(
                        (
                            f"{name}: {len(value)}-character value is not allowed",
                            ("header", name, kind),
                        )
                    )
        request_body = self._resolve(
            self.contract.document["paths"][template][method.lower()].get("requestBody")
        )
        if request_body:
            if not body:
                if request_body.get("required"):
                    found.append(("body: missing", ("body", "missing")))
            else:
                found.extend(self._body_violations(template, method, body))
        return found

    def _body_violations(self, template, method, body: bytes) -> list[tuple]:
        try:
            document = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return [("body: not valid JSON", ("body", "not JSON", _digest(body)))]
        pointer = template.replace("~", "~0").replace("/", "~1")
        validator = self._validator(
            {
                "$ref": f"urn:cra-contract#/paths/{pointer}/{method.lower()}"
                "/requestBody/content/application~1json/schema"
            }
        )
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        found = []
        for error in errors:
            where = ".".join(map(str, error.absolute_path)) or "(root)"
            if error.validator == "additionalProperties" and isinstance(error.instance, dict):
                allowed = error.schema.get("properties", {})
                detail = sorted(k for k in error.instance if k not in allowed)
            elif error.validator == "required":
                detail = error.message
            else:
                detail = _digest(json.dumps(error.instance, sort_keys=True).encode())
            found.append(
                (f"body {where}: {error.validator}", ("body", where, error.validator, detail))
            )
        return found


def _digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:16]


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _sendable(headers) -> bool:
    for name, value in headers:
        try:
            raw_name, raw_value = name.encode("ascii"), value.encode("latin-1")
        except UnicodeEncodeError:
            return False
        if not _LEGAL_HEADER_NAME(raw_name) or _ILLEGAL_HEADER_VALUE(raw_value):
            return False
    return True


@contextmanager
def _without_local_constants():
    """Hypothesis also draws string and number constants from the source of whatever
    non-library modules happen to be imported (tests, this repo), so the same seed gives
    a different corpus under pytest than from the CLI. Generate from the schema alone.
    This uses Hypothesis internals; requirements-dev.txt pins the version."""
    from hypothesis.internal.conjecture import providers
    from hypothesis.internal.constants_ast import Constants

    saved = providers._get_local_constants
    empty = Constants()
    providers._get_local_constants = lambda: empty
    providers.CONSTANTS_CACHE.cache.clear()
    try:
        yield
    finally:
        providers._get_local_constants = saved
        providers.CONSTANTS_CACHE.cache.clear()


def generate(budget=None, contract=None) -> tuple[list[MalformedRequest], GenerationStats]:
    """The deterministic malformed-request corpus, and what was dropped while building it."""
    import requests
    import schemathesis
    from hypothesis import HealthCheck, Phase, given, seed, settings
    from schemathesis import GenerationMode

    from acceptance.client import ROOT

    budget = BUDGET if budget is None else budget
    contract = contract or Contract()
    checker = RequestChecker(contract)
    schema = schemathesis.openapi.from_path(str(ROOT / "openapi.yaml"))
    stats = GenerationStats()
    corpus = []
    for operation, examples in budget.items():
        method, template = operation.split(" ", 1)
        strategy = schema[template][method].as_strategy(
            generation_mode=GenerationMode.NEGATIVE,
            headers={"Authorization": AUTH_PLACEHOLDER},
        )
        cases = []

        @seed(SEED)
        @settings(
            max_examples=examples,
            derandomize=True,
            database=None,
            deadline=None,
            phases=[Phase.generate],
            suppress_health_check=list(HealthCheck),
            print_blob=False,
        )
        @given(strategy)
        def collect(case):
            if "id" in case.path_parameters:
                case.path_parameters["id"] = TICKET_PLACEHOLDER
            cases.append(case)  # noqa: B023 - collect() runs before the loop moves on

        with _without_local_constants():
            collect()
        seen = set()
        faults = set()
        for case in cases:
            stats.generated[operation] += 1
            try:
                prepared = requests.Request(
                    **case.as_transport_kwargs(base_url="http://generated.invalid")
                ).prepare()
            except Exception:  # requests refuses some generated headers or bodies
                stats.unsendable[operation] += 1
                continue
            split = urlsplit(prepared.url)
            path = split.path + (f"?{split.query}" if split.query else "")
            headers = tuple(
                (k, v) for k, v in prepared.headers.items() if k.lower() not in DROPPED_HEADERS
            )
            body = prepared.body
            if isinstance(body, str):
                body = body.encode("utf-8")
            if not _sendable(headers):
                stats.unsendable[operation] += 1
                continue
            key = (path, headers, body)
            if key in seen:
                stats.duplicates[operation] += 1
                continue
            seen.add(key)
            violations = checker.violations(
                operation,
                case.path_parameters,
                parse_qsl(split.query, keep_blank_values=True),
                headers,
                body,
            )
            if not violations:
                stats.allowed_by_contract[operation] += 1
                continue
            fault = json.dumps(
                [operation, sorted({json.dumps(key) for _, key in violations})],
                ensure_ascii=False,
            )
            if fault in faults:
                stats.same_fault[operation] += 1
                continue
            faults.add(fault)
            stats.kept[operation] += 1
            descriptions = tuple(description for description, _ in violations)
            corpus.append(
                MalformedRequest(operation, method, path, headers, body, descriptions[:3], fault)
            )
    return corpus, stats


def fault_categories(request: MalformedRequest) -> set[tuple[str, str, str, str]]:
    """(operation, location, field, rule) for each invalid input, ignoring the invalid
    value itself. Array indices in body fields are shown as "*"."""
    operation, keys = json.loads(request.fault)
    categories = set()
    for key in map(json.loads, keys):
        if key[0] in ("path", "query"):
            categories.add((operation, key[0], key[1], "not allowed by the schema"))
        elif key[0] == "header":
            categories.add((operation, "header", key[1], key[2]))
        elif key[1] in ("missing", "not JSON"):
            categories.add((operation, "body", "(root)", key[1]))
        else:
            field_name = ".".join("*" if part.isdigit() else part for part in key[1].split("."))
            categories.add((operation, "body", field_name, key[2]))
    return categories


def fingerprint(corpus) -> str:
    """Short digest of the corpus, to show two runs sent the same requests."""
    import hashlib

    data = json.dumps([r.describe() for r in corpus], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


# --- Running the corpus -----------------------------------------------------------------


def seed_tickets(service: Service) -> dict[str, str]:
    """Create one ticket per role. aware_at is the target's current time, never later."""
    now = service.expect("GET", "/test/clock", 200, actor=None)["now"]
    fields = {
        "product": {"manufacturer": "Acme", "name": "Router", "affected_versions": ["1.0"]},
        "member_states": ["DE"],
        "vulnerability": {"cve_id": "CVE-2026-12345", "description": "Authentication bypass"},
        "exploit": {"description": "Confirmed exploitation"},
        "mitigations": {"taken": "Disabled access", "for_users": "Install update"},
        "sensitivity": "AMBER",
        "severity": "high",
        "impact": "Unauthorized access",
        "malicious_actor": "Unknown actor",
        "fix": {"available_at": now, "description": "Fixed", "fixed_versions": ["1.1"]},
    }
    received = service.create(title="AC-13 seed (received)", **fields)["id"]
    reportable = service.create(title="AC-13 seed (reportable)", **fields)["id"]
    service.triage(
        reportable, {"decision": "actively_exploited", "aware_at": now, "reason": "AC-13 seed"}
    )
    return {"received": received, "reportable": reportable}


def observable_state(service: Service, seeds: dict[str, str]) -> dict:
    """Everything the API shows about stored data: the list, and each seed in full."""
    state = {"tickets": service.list(), "seeds": {}}
    for role, ticket_id in seeds.items():
        quoted = quote(ticket_id, safe="")
        entry = {
            "ticket": service.get(ticket_id),
            "audit": service.expect(
                "GET", f"/tickets/{quoted}/audit", 200, template="/tickets/{id}/audit", actor=None
            ),
        }
        if entry["ticket"]["state"] == "reportable":
            entry["drafts"] = {
                kind: service.expect(
                    "GET",
                    f"/tickets/{quoted}/reports/{kind}",
                    200,
                    template="/tickets/{id}/reports/{type}",
                    actor=None,
                )
                for kind in ("early_warning", "notification", "final_report")
            }
        state["seeds"][role] = entry
    return state


def check_response(contract: Contract, request: MalformedRequest, response: Response) -> list:
    method, template = request.operation.split(" ", 1)
    documented = contract.document["paths"][template][method.lower()]["responses"]
    problems = []
    if response.status >= 500:
        problems.append(f"server error {response.status}")
    if response.status != request.expected_status:
        problems.append(f"status {response.status}, expected {request.expected_status}")
    if str(response.status) not in documented and response.status not in (401, 413):
        problems.append(f"status {response.status} is not documented for {request.operation}")
    media_type = response.content_type.split(";", 1)[0].strip().lower()
    if media_type != PROBLEM:
        problems.append(f"media type does not match {PROBLEM}")
        return problems
    try:
        if str(response.status) in documented:
            body = contract.validate(method, template, response)
        else:
            body = _validate_global(contract, response)
    except (AssertionError, ValueError):
        problems.append("body does not match the contract")
        return problems
    if isinstance(body, dict) and body.get("status") != response.status:
        problems.append(f"problem status does not match HTTP {response.status}")
    return problems


def _validate_global(contract, response):
    name = {401: "Unauthorized", 413: "PayloadTooLarge"}.get(response.status)
    if name is None:
        raise AssertionError(f"no contract schema for status {response.status}")
    body = json.loads(response.body)
    errors = list(
        Draft202012Validator(
            {
                "$ref": f"urn:cra-contract#/components/responses/{name}/content/"
                "application~1problem+json/schema"
            },
            registry=contract.registry,
        ).iter_errors(body)
    )
    if errors:
        raise AssertionError("body does not match the global error schema")
    return body


def run(service: Service, send_raw, corpus, seeds: dict[str, str], progress=None) -> dict:
    """Send every request; return per-request results. Re-seeds a role after a 2xx change."""
    contract = service.contract
    seeds = dict(seeds)
    results = []
    for index, request in enumerate(corpus):
        role = SEED_ROLE.get(request.operation)
        path = request.path
        if role:
            path = path.replace(
                f"/tickets/{TICKET_PLACEHOLDER}", "/tickets/" + quote(seeds[role], safe=""), 1
            )
        try:
            response = send_raw(request.method, path, dict(request.headers), request.body)
        except (OSError, URLError, HTTPException) as error:
            results.append(
                {
                    "request": request,
                    "status": None,
                    "problems": [f"transport failed ({type(error).__name__})"],
                }
            )
            continue
        problems = check_response(contract, request, response)
        result = {"request": request, "status": response.status, "problems": problems}
        if problems:
            result["response"] = {
                "status": response.status,
                "problem_media_type": (
                    response.content_type.split(";", 1)[0].strip().lower() == PROBLEM
                ),
                "body_bytes": len(response.body),
            }
        results.append(result)
        if 200 <= response.status < 300 and request.method in MUTATING and role:
            # The seed may have changed state; keep the rest of this operation meaningful.
            seeds[role] = seed_tickets(service)[role]
        if progress and (index + 1) % 100 == 0:
            progress(index + 1, len(corpus))
    return {"results": results, "seeds": seeds}


def summarize(corpus, stats: GenerationStats, outcome, before, after, seconds) -> dict:
    results = outcome["results"]
    by_status = Counter(str(r["status"]) for r in results)
    by_operation = defaultdict(Counter)
    for r in results:
        by_operation[r["request"].operation][str(r["status"])] += 1
    failures = [
        {**r["request"].describe(), "problems": r["problems"], "response": r.get("response")}
        for r in results
        if r["problems"]
    ]
    return {
        "corpus_fingerprint": fingerprint(corpus),
        "distinct_invalid_inputs": len({r.fault for r in corpus}),
        "fault_categories": len(set().union(*(fault_categories(r) for r in corpus))),
        "fault_categories_by_operation": {
            op: sorted(
                f"{location} {name}: {rule}"
                for (cat_op, location, name, rule) in set().union(
                    *(fault_categories(r) for r in corpus)
                )
                if cat_op == op
            )
            for op in sorted({r.operation for r in corpus})
        },
        "unique_requests_generated": sum(stats.kept.values()) + sum(stats.same_fault.values()),
        "requests_sent": len(results),
        "seconds": round(seconds, 1),
        "by_status": dict(sorted(by_status.items())),
        "by_operation": {op: dict(sorted(c.items())) for op, c in sorted(by_operation.items())},
        "server_errors": sum(1 for r in results if r["status"] is not None and r["status"] >= 500),
        "failures": failures,
        "data_unchanged": before == after,
        "generation": {
            "seed": SEED,
            "budget": BUDGET,
            "not_generated": UNGENERATED,
            **{name: dict(getattr(stats, name)) for name in GENERATION_FIELDS},
        },
    }


def print_summary(summary, out=sys.stdout, show=10):
    print(
        f"Sent {summary['requests_sent']} malformed requests in {summary['seconds']} s "
        f"(corpus {summary['corpus_fingerprint']}): {summary['distinct_invalid_inputs']} "
        f"distinct invalid inputs, {summary['fault_categories']} fault categories "
        "(operation, location/field, rule)",
        file=out,
    )
    print("By status: " + ", ".join(f"{k}: {v}" for k, v in summary["by_status"].items()), file=out)
    for op, counts in summary["by_operation"].items():
        print(f"  {op}: " + ", ".join(f"{k}: {v}" for k, v in counts.items()), file=out)
    print(f"Server errors (5xx): {summary['server_errors']}", file=out)
    print(f"Contract mismatches: {len(summary['failures'])}", file=out)
    for failure in summary["failures"][:show]:
        print(json.dumps(failure, ensure_ascii=False)[:1500], file=out)
    print(f"Stored data unchanged: {'yes' if summary['data_unchanged'] else 'NO'}", file=out)


# --- CLI --------------------------------------------------------------------------------


def http_sender(transport: HTTPTransport):
    def send(method, path, headers, body):
        request = Request(transport.base_url + path, data=body, method=method, headers=headers)
        request.add_unredirected_header("Authorization", "Bearer " + transport.token)
        try:
            response = transport.opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return Response(
                response.code, response.headers.get("Content-Type", ""), response.read()
            )

    return send


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m acceptance.malformed",
        description="AC-13: send generated malformed requests to a deployment and check that "
        "each gets the documented problem+json 4xx and nothing is stored.",
    )
    parser.add_argument("--base-url", default=os.environ.get("CRA_BASE_URL"))
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset the target before and after the run. A reset deletes ALL tickets, reports, "
        "and audit entries on the whole deployment, for every client.",
    )
    parser.add_argument("--out", help="Write the JSON summary here")
    parser.add_argument("--show", type=int, default=10, help="Mismatches to print (default 10)")
    args = parser.parse_args(argv)

    if not args.base_url:
        parser.error("provide --base-url or CRA_BASE_URL")
    token = os.environ.get("CRA_API_TOKEN")
    if not token:
        parser.error("set CRA_API_TOKEN in the environment")
    try:
        transport = HTTPTransport(args.base_url, token)
    except ValueError as error:
        parser.error(str(error))
    service = Service(transport)
    if sys.version_info[:2] != PYTHON:
        print(
            f"Refusing to run: this needs Python {PYTHON[0]}.{PYTHON[1]} (found "
            f"{sys.version_info.major}.{sys.version_info.minor}). Other versions generate a "
            "different corpus from the one CI checks.",
            file=sys.stderr,
        )
        return 2

    corpus, stats = generate()
    print(
        f"Generated {len(corpus)} malformed requests, each with a different invalid input "
        f"(seed {SEED}, corpus {fingerprint(corpus)}).",
        flush=True,
    )
    expected = expected_corpus()
    if expected is None:
        print(
            f"WARNING: no corpus is recorded for {sys.platform}, so this run can't be "
            "checked against a recorded one.",
            file=sys.stderr,
            flush=True,
        )
    elif (len(corpus), fingerprint(corpus)) != expected:
        print(
            f"WARNING: this is not the corpus recorded for {sys.platform} ({expected[0]} "
            f"requests, {expected[1]}). Install requirements-dev.txt exactly; results here "
            "are not comparable with the recorded corpus.",
            file=sys.stderr,
            flush=True,
        )

    try:
        existing = service.list()  # also checks the URL and token before anything changes
    except AssertionError as error:
        print(f"Preflight GET /tickets failed ({type(error).__name__})", file=sys.stderr)
        return 2
    if args.reset:
        print(
            "--reset: POST /test/reset deletes ALL tickets, reports, and audit entries on "
            f"{transport.base_url}, for every client."
        )
        service.expect("POST", "/test/reset", 204, actor=None)
    elif existing:
        print(
            f"Refusing to run: {transport.base_url} holds {len(existing)} tickets. The run "
            "adds seed tickets, so it needs an empty target, or --reset (which wipes the "
            "whole deployment).",
            file=sys.stderr,
        )
        return 2

    started = time.monotonic()
    summary = None
    cleanup_failed = False
    outcome = {"seeds": {}}
    try:
        seeds = seed_tickets(service)
        outcome["seeds"] = seeds
        before = observable_state(service, seeds)
        outcome = run(
            service,
            http_sender(transport),
            corpus,
            seeds,
            progress=lambda n, total: print(f"  {n}/{total} sent", flush=True),
        )
        after = observable_state(service, seeds)
        summary = summarize(corpus, stats, outcome, before, after, time.monotonic() - started)
        summary["base_url"] = transport.base_url
        print_summary(summary, show=args.show)
    finally:
        try:
            if args.out and summary is not None:
                with open(args.out, "w", encoding="utf-8") as handle:
                    json.dump(summary, handle, indent=2, ensure_ascii=False)
                print(f"Wrote {args.out}")
        finally:
            if summary is None:
                print("The run stopped before it finished; see the error below.", file=sys.stderr)
            if args.reset:
                # Each step runs even if the other fails, and a failure here is reported
                # without replacing an error already on its way out.
                for method, path in (("POST", "/test/reset"), ("DELETE", "/test/clock")):
                    try:
                        service.expect(method, path, 204, actor=None)
                        print(f"Cleanup: {method} {path} done.")
                    except Exception as error:
                        cleanup_failed = True
                        print(
                            f"Cleanup: {method} {path} FAILED ({type(error).__name__})",
                            file=sys.stderr,
                        )
            elif outcome["seeds"]:
                print(f"Left seed tickets on the target: {', '.join(outcome['seeds'].values())}")

    ok = (
        not summary["failures"]
        and summary["data_unchanged"]
        and summary["distinct_invalid_inputs"] >= MINIMUM_FAULTS
        and not cleanup_failed
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
