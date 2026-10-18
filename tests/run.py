"""Run the pod's shared test file against a deployment. Usage: python3 tests/run.py tests/model-d.json BASE_URL, with the token in CRA_API_TOKEN. Needs only the Python standard library."""
import json, os, sys, urllib.request, urllib.error

path, base = sys.argv[1], sys.argv[2].rstrip("/")
doc = json.load(open(path))
token = os.environ["CRA_API_TOKEN"]
default_body = doc["conventions"]["default_ticket_body"]

def call(method, url, body=None, actor=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    if actor:
        req.add_header("X-Actor", "shared-tests")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)

def match(expected, actual, where):
    errs = []
    for k, v in expected.items():
        if not isinstance(actual, dict) or k not in actual:
            errs.append(f"{where}.{k}: missing"); continue
        if isinstance(v, dict):
            errs += match(v, actual[k], f"{where}.{k}")
        elif actual[k] != v:
            errs.append(f"{where}.{k}: expected {v!r}, got {actual[k]!r}")
    return errs

failed = 0
for case in doc["cases"]:
    call("POST", "/test/reset", actor=False)
    ids, errs = {}, []
    for i, step in enumerate(case["steps"], 1):
        a = step["action"]
        if a == "set_clock":
            st, body = call("PUT", "/test/clock", {"now": step["now"]}, actor=False)
        elif a == "submit_ticket":
            st, body = call("POST", "/tickets", step.get("body", default_body))
            if st == 201: ids[step["save_as"]] = body["id"]
        elif a == "triage":
            st, body = call("POST", f"/tickets/{ids[step['ticket']]}/triage", step["body"])
        elif a == "update":
            st, body = call("PATCH", f"/tickets/{ids[step['ticket']]}", step["body"])
        elif a == "get_ticket":
            st, body = call("GET", f"/tickets/{ids[step['ticket']]}", actor=False)
        else:
            errs.append(f"step {i}: unknown action {a}"); continue
        exp = step.get("expect")
        if not exp: continue
        if st != exp["status"]:
            errs.append(f"step {i} ({a}): expected status {exp['status']}, got {st}")
        if "body" in exp:
            errs += [f"step {i} ({a}): {e}" for e in match(exp["body"], body, "body")]
        if "error_fields" in exp:
            got = [e["field"] for e in (body or {}).get("errors", [])]
            if got != exp["error_fields"]:
                errs.append(f"step {i} ({a}): expected error fields {exp['error_fields']}, got {got}")
    print(f"{case['id']}: {'PASS' if not errs else 'FAIL'}")
    for e in errs: print("   ", e)
    failed += bool(errs)
call("POST", "/test/reset", actor=False); call("DELETE", "/test/clock", actor=False)
print(f"{len(doc['cases']) - failed}/{len(doc['cases'])} cases passed")
sys.exit(1 if failed else 0)
