# Testing Team 17's service

How to test Team 17's evaluation deployment, and how it behaves where the shared contract
leaves room for interpretation. Maintained by Team 17. For what's implemented, see the
[status chart](../README.md#status).

**Base URL:** https://team17-prod-7zneiwx4ba-uc.a.run.app

## Before you start

- **Who uses it:** this deployment is for Team 21 and the course staff. Team 17 tests on
  its own development server and never resets this one. When the course staff review it,
  Team 21 shares it with them, so don't run a suite while the staff are reviewing.
- **Auth:** `GET /health` is public. Every other request needs
  `Authorization: Bearer <token>`, with the token Team 17 sent you privately. Keep it in an
  environment variable, never in a file you commit.
- **X-Actor:** every request that changes ticket data (submit, update, triage) needs an
  `X-Actor` header naming the person. Missing, empty, or whitespace-only returns 400. The
  `/test/*` endpoints don't need it.
- **Use exactly this URL.** Other Cloud Run hostnames for the same service return 400.
- **Cold start:** it scales to zero, so the first request after a quiet period can take a
  few seconds. Send `GET /health` first and don't count it toward timings.
- **Leave it clean:** when you finish, `POST /test/reset` and then `DELETE /test/clock`, so
  it's empty and on real time.

## A first run

This walks through Model D example **E1**: a report arrives Friday at 17:00, and triage on
Monday at 09:00 confirms exploitation that began at receipt. Set `TOKEN` in your shell
first.

```bash
BASE=https://team17-prod-7zneiwx4ba-uc.a.run.app
AUTH="Authorization: Bearer $TOKEN"

# 1. Reset, then freeze the clock at the receipt time
curl -s -X POST "$BASE/test/reset" -H "$AUTH"                          # 204
curl -s -X PUT "$BASE/test/clock" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"now": "2026-10-02T17:00:00Z"}'                                  # {"now": "2026-10-02T17:00:00Z"}

# 2. Submit a ticket (note the X-Actor header)
curl -s -X POST "$BASE/tickets" -H "$AUTH" -H "X-Actor: team21-tester" \
  -H "Content-Type: application/json" \
  -d '{"source": "customer", "title": "Friday report", "description": "E1",
       "product": {"manufacturer": "Acme", "name": "Router OS"}}'      # 201; note the "id"

# 3. Move the clock to Monday 09:00 and triage with awareness at receipt
curl -s -X PUT "$BASE/test/clock" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"now": "2026-10-05T09:00:00Z"}'
curl -s -X POST "$BASE/tickets/<id>/triage" -H "$AUTH" -H "X-Actor: team21-tester" \
  -H "Content-Type: application/json" \
  -d '{"decision": "actively_exploited", "aware_at": "2026-10-02T17:00:00Z",
       "reason": "Logs show exploitation"}'                             # 200
```

Expected in the triage response (Model D, E1):

| Obligation | `status` | `due_at` |
|---|---|---|
| `early_warning` | `overdue` | `2026-10-03T17:00:00Z` |
| `notification` | `pending` | `2026-10-05T17:00:00Z` |
| `final_report` | `awaiting_fix` | `null` |

`state` is `reportable`, and `next_due_at` is `2026-10-03T17:00:00Z`. Status is computed
from the clock on every read, so moving the clock back to `2026-10-03T16:00:00Z` turns the
early warning back to `pending`.

## How we handle gray areas

Where the shared contract is silent or ambiguous, Team 17's service behaves as follows. If
your suite expects something different, raise it as a spec question rather than a bug, so
the pod can settle it for both teams.

### Errors and requests

| Question | Team 17's behavior |
|---|---|
| Error precedence | 401, 413, 404, 400, 409, 422, as in the contract. A wrong method returns 405 right after the 401 check, even for an unknown ticket (`PUT /tickets/<unknown>/audit` is 405, not 404). |
| Does 413 (over 64 KB) apply to routes without a body? | Yes, for `POST`, `PUT`, `PATCH`, and `DELETE`, including `/close` and `/test/reset`. |
| A `GET` request with a body | Never reaches the service. Google's front end rejects any `GET` that has a body, even a small one: over HTTP/2 the stream is reset with no response, and over HTTP/1.1 you get Google's HTML 400, not problem+json. Don't send bodies on `GET`. |
| A body sent to a route that defines none | Ignored, not rejected. |
| A chunked or streamed body with no `Content-Length` | Accepted. A chunked HTTP/1.1 body and a streamed HTTP/2 body both worked (201). |
| An unknown or malformed ticket ID or report type | 404. Ticket IDs are opaque strings; don't assume a format. |
| What's public | Only `GET /health`. `HEAD /health` needs the token. |
| How `errors[].field` is written | A dotted path with `[n]` for list items: `product.affected_versions[1]`. Body-level and JSON errors use `body`, header errors use `X-Actor`, query errors use the parameter name. |

### Time

| Question | Team 17's behavior |
|---|---|
| Timestamp variants | RFC 3339 with seconds and an offset. Lowercase `t`/`z` and `-00:00` are accepted. A space separator, a missing offset, missing seconds, the leap second `:60`, and values that overflow when converted to UTC return 400. |
| Fractional seconds | Accepted and truncated, never rounded (`09:00:00.789Z` becomes `09:00:00Z`). |
| Does reset release a frozen clock? | No. Reset deletes tickets, reports, and audit entries only. `DELETE /test/clock` returns to real time. |

### Tickets

| Question | Team 17's behavior |
|---|---|
| Triage as `invalid` or `not_actively_exploited` with an `aware_at` | Accepted (200). `aware_at` is ignored and stays `null`. Open pod question Q1. |
| `POST /tickets` with `fix.available_at` later than now | Accepted (201). The contract lists no 422 for submit. Open pod question Q7. |
| `PATCH` and a stored future fix time | Only a fix time the request sends is checked against now. Editing other fields never fails because of a stored one. |
| `PATCH` merging | Nested objects merge one level deep, lists are replaced whole, and `null` clears an optional field. `title`, `description`, and `product.manufacturer`/`name` can't be cleared (400). |
| `GET /tickets` order | `next_due_at` ascending, tickets with no deadline last, then by `id`. |
| `GET /tickets?state=…&obligation_status=…` | Not implemented yet (Phase 4). The filters are ignored and every ticket is returned. |
| Reporter details | Only in `GET /tickets/{id}`. Never in lists, other responses, audit entries, or errors. |

## For coding agents

Agents testing this deployment also follow the pod's [`AGENTS.md`](../AGENTS.md). These
mistakes come up most often when an agent tests someone else's server:

1. **Never print the token.** `curl -v`, `--trace`, and verbose HTTP client logging print the
   full `Authorization` header, so the token ends up in your transcript or CI logs. Read it
   from an environment variable, and if you need to debug, log the status and body only.
2. **Run tests one at a time.** Every test starts with a reset, so parallel runners
   (`pytest -n`, concurrent requests across tests) wipe each other's data and fail at random.
3. **Expected values come from the spec, not from our responses.** If an assertion fails,
   don't copy what our server returned into the test. Check the gray-areas tables above; if
   the difference is listed there, it's a spec question for the pod, not something to paper
   over.
4. **A slow first request isn't an outage.** Send `GET /health` first and allow up to 30
   seconds for it. Use normal timeouts after that.
5. **Clean up when you finish:** `POST /test/reset`, then `DELETE /test/clock`.
6. **Ask before running anything** against this deployment, and report problems to your
   person, who raises them with Team 17.

## Reporting a problem

Open an issue in this repo, or message Team 17, with:
- the request (method, path, headers **without the token**, body)
- the expected and actual status code and body
- the time you sent it (UTC) and what the test clock was set to

Never include the token. If you think it has leaked, tell us and we'll replace it.
