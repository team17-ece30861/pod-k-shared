"""Session-bound design evaluations, independent of conformance and pass rate."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DESIGN_VERSION = "team17-phase5-design-v1"
MANIFEST_DIGEST = "8af77d04b7b44592d8f3bf2acf2c79e6de107475fed7652573bc005a28683734"
MANIFEST_PATH = Path(__file__).with_name("model_requirements.json")
INVENTORY_COUNTS = {"A": 8, "B": 5, "C": 4, "D": 8, "X": 10, "I": 2, "P": 2, "T": 5}
ACTIVE_RECORDER = ContextVar("model_recorder", default=None)


@lru_cache(maxsize=1)
def manifest():
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    normalized = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if sha256(normalized).hexdigest() != MANIFEST_DIGEST:
        raise ValueError("Model inventory changed without a design version")
    expected = {
        f"{prefix}-{number:02d}"
        for prefix, count in INVENTORY_COUNTS.items()
        for number in range(1, count + 1)
    }
    requirements = document["requirements"]
    if document["version"] != DESIGN_VERSION or document["schema_version"] != 1:
        raise ValueError("Unsupported model manifest version")
    if len(requirements) != len(expected) or {row["id"] for row in requirements} != expected:
        raise ValueError("Model requirement inventory changed without a design version")
    seen = set()
    for requirement in requirements:
        if not requirement["description"] or not requirement["scenarios"]:
            raise ValueError("Model requirements need descriptions and scenarios")
        for scenario in requirement["scenarios"]:
            if not scenario["id"].startswith(requirement["id"] + "."):
                raise ValueError("Scenario does not belong to its requirement")
            if scenario["id"] in seen:
                raise ValueError("Repeated model scenario")
            seen.add(scenario["id"])
            if scenario["test"] is not None and canonical_test(scenario["test"]) is None:
                raise ValueError("Invalid model test binding")
    return document


def canonical_test(test_id):
    if not isinstance(test_id, str) or "::" not in test_id:
        return None
    source, remainder = test_id.replace("\\", "/").split("::", 1)
    root = Path(__file__).parent.as_posix() + "/"
    if source.startswith(root):
        source = source[len(root) :]
    elif source.startswith("acceptance/"):
        source = source.removeprefix("acceptance/")
    if "/" in source or not source.startswith("test_") or not source.endswith(".py"):
        return None
    return source + "::" + remainder


def scenario_bindings():
    return {
        scenario["id"]: scenario["test"]
        for requirement in manifest()["requirements"]
        for scenario in requirement["scenarios"]
    }


class ModelJournal:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.available = True
        self.events = []
        if self.path is not None:
            try:
                self.path.write_text(
                    json.dumps({"kind": "header", "version": DESIGN_VERSION}) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                self.available = False

    def _append(self, entry):
        if self.path is not None and self.available:
            try:
                with self.path.open("a", encoding="utf-8") as destination:
                    destination.write(json.dumps(entry) + "\n")
            except OSError:
                self.available = False

    def invalidate(self):
        self._append({"kind": "unavailable"})
        self.available = False

    def record(self, event):
        self.events.append(event.copy())
        self._append({"kind": "evaluation", **event})


class ModelRecorder:
    def __init__(self, test_id, journal):
        self.test_id = canonical_test(test_id)
        self.journal = journal
        self.available = True
        self.events = []

    def record(self, scenario_id, satisfied):
        expected = scenario_bindings().get(scenario_id)
        if expected is None or expected != self.test_id:
            self.available = False
            self.journal.invalidate()
            return
        event = {"scenario": scenario_id, "satisfied": satisfied}
        self.events.append(event)
        self.journal.record(event)

    def evidence(self):
        return {
            "version": DESIGN_VERSION,
            "available": self.available and self.journal.available,
            "events": self.events.copy(),
        }


@contextmanager
def record_model_test(test_id, journal):
    recorder = ModelRecorder(test_id, journal)
    token = ACTIVE_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        ACTIVE_RECORDER.reset(token)


SCALARS = (str, int, float, bool, type(None))
VALUE_LIMIT = 60


def _short(value):
    text = repr(value)
    return text if len(text) <= VALUE_LIMIT else text[: VALUE_LIMIT - 3] + "..."


def _difference(actual, expected):
    """Says how two values differ without echoing nested bodies, which can hold reporter
    details and are handed to other teams in reports."""
    if isinstance(actual, SCALARS) and isinstance(expected, SCALARS):
        return f"expected {_short(expected)}, got {_short(actual)}"
    if isinstance(actual, dict) and isinstance(expected, dict):
        parts = []
        for key in sorted(set(actual) | set(expected), key=str):
            if key not in actual:
                parts.append(f"{key} missing")
            elif key not in expected:
                parts.append(f"{key} unexpected")
            elif actual[key] != expected[key]:
                old, new = expected[key], actual[key]
                if isinstance(old, SCALARS) and isinstance(new, SCALARS):
                    parts.append(f"{key} expected {_short(old)}, got {_short(new)}")
                else:
                    parts.append(f"{key} differs")
        return "; ".join(parts) or "differs"
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return f"expected {len(expected)} items, got {len(actual)}"
        for index, (new, old) in enumerate(zip(actual, expected, strict=True)):
            if new != old:
                return f"item {index} differs"
        return "differs"
    return f"expected {type(expected).__name__}, got {type(actual).__name__}"


class OracleChecks:
    """One model oracle group: every comparison is evaluated (no short circuit), and a
    failure names each comparison that failed, e.g. "state: expected 'received', got
    'triaged'". Use: `assert all(checks.results), checks.summary()` inside model_scenario."""

    def __init__(self):
        self.results = []
        self.failures = []

    def equal(self, label, actual, expected):
        satisfied = bool(actual == expected)
        self.results.append(satisfied)
        if not satisfied:
            self.failures.append(f"{label}: {_difference(actual, expected)}")

    def true(self, label, condition):
        satisfied = bool(condition)
        self.results.append(satisfied)
        if not satisfied:
            self.failures.append(f"{label}: not satisfied")

    def summary(self):
        return "; ".join(self.failures)


@contextmanager
def model_scenario(scenario_id, *, comparisons=None):
    if scenario_id not in scenario_bindings():
        raise ValueError("Unknown model scenario")
    recorder = ACTIVE_RECORDER.get()
    if comparisons is not None and (
        not isinstance(comparisons, (list, tuple))
        or not comparisons
        or any(type(comparison) is not bool for comparison in comparisons)
    ):
        if recorder is not None:
            recorder.available = False
            recorder.journal.invalidate()
        raise ValueError("Model oracle groups require evaluated boolean comparisons")
    if not __debug__ and recorder is not None:
        recorder.available = False
        recorder.journal.invalidate()
        recorder = None
    try:
        yield
    except AssertionError:
        if recorder is not None:
            recorder.record(scenario_id, False)
        raise
    else:
        if recorder is not None:
            recorder.record(scenario_id, True)


def read_model_journal(path):
    unavailable = {"version": DESIGN_VERSION, "available": False, "events": []}
    try:
        content = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return unavailable
    lines = content.splitlines(keepends=True)
    if not lines or not lines[0].endswith("\n"):
        return unavailable
    try:
        header = json.loads(lines[0])
        if header != {"kind": "header", "version": DESIGN_VERSION}:
            return unavailable
        events = []
        for index, line in enumerate(lines[1:], start=1):
            if index == len(lines) - 1 and not line.endswith("\n"):
                break
            entry = json.loads(line)
            if set(entry) != {"kind", "scenario", "satisfied"}:
                return unavailable
            if entry.pop("kind") != "evaluation":
                return unavailable
            events.append(entry)
    except (ValueError, TypeError, AttributeError):
        return unavailable
    return {"version": DESIGN_VERSION, "available": True, "events": events}


def canonical_base_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            return None
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None and (parts.scheme, port) not in {("https", 443), ("http", 80)}:
            host += f":{port}"
        return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
    except ValueError:
        return None


def model_measure(evidence, planned, mapping_url):
    basis = f"Design {DESIGN_VERSION}; "

    def unavailable(reason):
        return {"covered": None, "total": None, "basis": basis + reason}

    mapping = canonical_base_url(mapping_url)
    if mapping is None:
        return unavailable("mapping not yet published/configured; see docs/model-coverage.md")
    basis += f"mapping: {mapping}; "
    if not isinstance(evidence, dict) or evidence.get("version") != DESIGN_VERSION:
        return unavailable("session evaluation evidence is unavailable or has another version")
    if evidence.get("available") is not True or not isinstance(evidence.get("events"), list):
        return unavailable("session evaluation collector is unavailable")
    try:
        document = manifest()
        bindings = scenario_bindings()
        planned_ids = {canonical_test(test_id) for test_id in planned}
        evaluated = set()
        for event in evidence["events"]:
            if not isinstance(event, dict) or set(event) != {"scenario", "satisfied"}:
                return unavailable("session evaluation evidence is invalid")
            scenario_id = event["scenario"]
            if not isinstance(scenario_id, str) or type(event["satisfied"]) is not bool:
                return unavailable("session evaluation evidence is invalid")
            binding = bindings.get(scenario_id)
            if binding is None or binding not in planned_ids:
                return unavailable("evaluation does not match this suite's approved test bindings")
            evaluated.add(scenario_id)
        covered = sum(
            all(scenario["id"] in evaluated for scenario in requirement["scenarios"])
            for requirement in document["requirements"]
        )
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable("design inventory or session evaluation evidence is invalid")
    return {
        "covered": covered,
        "total": len(document["requirements"]),
        "basis": basis + "all required scenarios evaluated per requirement; not a pass rate",
    }
