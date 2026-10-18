import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes = field(repr=False)


class Contract:
    def __init__(self):
        self.document = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
        self.registry = Registry().with_resource(
            "urn:cra-contract",
            Resource.from_contents(self.document, default_specification=DRAFT202012),
        )

    def validate(self, method, path, response):
        definition = self.document["paths"][path][method.lower()]["responses"][str(response.status)]
        pointer = definition.get("$ref") or (
            f"#/paths/{path.replace('~', '~0').replace('/', '~1')}/{method.lower()}"
            f"/responses/{response.status}"
        )
        if "$ref" in definition:
            definition = (
                self.registry.resolver("urn:cra-contract").lookup(definition["$ref"]).contents
            )
        content = definition.get("content")
        if not content:
            if response.status == 204:
                assert response.body == b""
            return None
        media_type = response.content_type.split(";", 1)[0].strip().lower()
        assert media_type in content, f"Unexpected response media type: {media_type}"
        body = json.loads(response.body)
        media_pointer = media_type.replace("~", "~0").replace("/", "~1")
        validator = Draft202012Validator(
            {"$ref": f"urn:cra-contract{pointer}/content/{media_pointer}/schema"},
            registry=self.registry,
            format_checker=FormatChecker(),
        )
        errors = list(validator.iter_errors(body))
        if errors:
            raise AssertionError(
                "; ".join(
                    f"{'.'.join(map(str, error.absolute_path)) or 'body'}: {error.validator} failed"
                    for error in errors
                )
            )
        return body


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    def __init__(self, base_url: str, token: str):
        parsed = urlsplit(base_url)
        if not parsed.hostname:
            raise ValueError("base URL must include a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "base URL must not include credentials, query parameters, or a fragment"
            )
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("use HTTPS, or HTTP for a loopback development service")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirect())

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
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Actor"] = actor
        request = Request(self.base_url + path, data=data, method=method, headers=headers)
        if authenticated:
            request.add_unredirected_header(
                "Authorization", "Bearer " + (token if token is not None else self.token)
            )
        try:
            response = self.opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return Response(
                response.code, response.headers.get("Content-Type", ""), response.read()
            )


class Service:
    def __init__(self, transport):
        self.transport = transport
        self.contract = Contract()

    def expect(self, method, path, status, *, template=None, **kwargs):
        response = self.transport.send(method, path, **kwargs)
        assert response.status == status, (
            f"{method} {template or path}: expected {status}, got {response.status}"
        )
        return self.contract.validate(method, template or path, response)

    def fix_clock(self, value):
        return self.expect("PUT", "/test/clock", 200, body={"now": value}, actor=None)

    def create(self, **overrides):
        body = {
            "source": "external_researcher",
            "title": "Authentication bypass",
            "description": "Simulated vulnerability for Phase 3 tests.",
            "product": {
                "manufacturer": "Acme",
                "name": "Router OS",
                "affected_versions": ["4.1.0"],
            },
            "reporter": {"name": "Simulated Reporter", "contact": "reporter@example.invalid"},
            **overrides,
        }
        result = self.expect("POST", "/tickets", 201, body=body)
        assert "reporter" not in result
        return result

    def get(self, id):
        return self.expect("GET", "/tickets/" + quote(id, safe=""), 200, template="/tickets/{id}")

    def triage(self, id, body, status=200, **kwargs):
        return self.expect(
            "POST",
            "/tickets/" + quote(id, safe="") + "/triage",
            status,
            template="/tickets/{id}/triage",
            body=body,
            **kwargs,
        )

    def list(self):
        result = self.expect("GET", "/tickets", 200)
        assert all("reporter" not in ticket for ticket in result)
        return result
