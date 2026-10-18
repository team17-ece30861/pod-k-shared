# pod-k-shared

Shared between **Team 17** and **Team 21** (ECE 30861, Pod K) for the CRA Article 14
reporting service. Each team builds its own implementation of the pod spec agreed on
September 25, 2026. This repo holds what both teams depend on: where each evaluation
deployment lives, how to test it, and (later) the shared contract and test file.

## Deployments (the pod file)

Phase 2, Q5.3: each team publishes its evaluation deployment's base URL here.

| Team | Base URL | Contact | Status |
|---|---|---|---|
| Team 17 | https://team17-prod-7zneiwx4ba-uc.a.run.app | Quincy DeKlerk (platform) | Phase 3 operations live; see below |
| Team 21 | *to be added by Team 21* | | |

### Team 17's deployment

**Who uses it.** This URL is our evaluation deployment, for Team 21 and the course staff.
Team 17 runs its own testing on a separate development server, so we never reset this
one. When the course staff review it, Team 21 shares it with them. Avoid running a suite
(which resets everything) while the staff are reviewing.

**What's implemented (as of 2026-10-18).** All Phase 3 operations are implemented, and our
Phase 3 acceptance suite passed 55/55 against this URL on 2026-10-18. Cross-team testing
hasn't happened yet, so Phase 3 isn't finished until Team 21's suite has run against it.

| Operation | Status |
|---|---|
| `GET /health` | Implemented |
| `POST /tickets`, `GET /tickets/{id}`, `PATCH /tickets/{id}` | Implemented |
| `POST /tickets/{id}/triage` | Implemented |
| `GET /tickets/{id}/audit` | Implemented |
| `POST /test/reset`, `GET`/`PUT`/`DELETE /test/clock` | Implemented |
| `GET /tickets` | Implemented without filters. `state` and `obligation_status` are ignored until Phase 4. |
| `GET /tickets/{id}/reports/{type}`, `POST .../submission`, `POST /tickets/{id}/close` | Not yet (Phase 4) |

**Connection details.**
- `GET /health` is public. Everything else needs `Authorization: Bearer <token>`.
- Use exactly the URL above. Other Cloud Run hostnames for the same service return 400.
- It scales to zero, so the first request after a quiet period takes a few seconds.
- After a test run, leave it clean: `POST /test/reset`, then `DELETE /test/clock` to return
  it to real time.

## Tokens

**Tokens never go in this repo**, not in files, issues, or commit messages. Each team
sends the other its token privately. Team 17 issues Team 21 a token of its own, so it can
be replaced without affecting anyone else.

## Testing another team's deployment

From Phase 2, Q5.3:
- Every test starts with `POST /test/reset` and then `PUT /test/clock`. Tests don't depend
  on each other and can run in any order.
- Reset clears the whole deployment, so only one suite may run against a deployment at a
  time.
- The test endpoints stay enabled on the deployment used for evaluation.

## Planned contents

| What | Source | Status |
|---|---|---|
| Shared contract, `openapi.yaml` | Phase 2, Section 5 | Draft lives in Team 17's repo. Moves here once both teams freeze the Phase 3 operations. |
| Shared test file: inputs and hand-calculated outputs | Phase 2, Q4.2 | Starts with the Model D deadline examples. Each expected output is calculated by one team and checked by the other before it's added. |

## Changing this repo

- All changes arrive as pull requests.
- **Any change to the shared contract or the shared test file needs approval from someone
  on the other team** before it merges.
- Each team may update its own row in the deployments table, and its own deployment
  section, without the other team's approval.
