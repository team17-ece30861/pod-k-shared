# pod-k-shared

Shared between **Team 17** and **Team 21** (ECE 30861, Pod K) for the CRA Article 14
reporting service. Each team builds its own implementation of the pod spec agreed on
September 25, 2026. This repo holds what both teams depend on: the specification, where
each evaluation deployment lives, and how to test it.

## Specification

- [`spec.md`](spec.md): what every Pod K system must accept and return, and what counts as
  correct behavior (requirements, models, deadline examples, acceptance criteria).
- [`openapi.yaml`](openapi.yaml): the interface contract. Currently `0.1.0-draft`; the
  Phase 3 operations are being frozen with both teams.

## Deployments (the pod file)

Phase 2, Q5.3: each team publishes its evaluation deployment's base URL here.

| Team | Base URL | Contact | Status |
|---|---|---|---|
| Team 17 | https://team17-prod-7zneiwx4ba-uc.a.run.app | Quincy DeKlerk (platform) | See [Status](#status) |
| Team 21 | *to be added by Team 21* | | See [Status](#status) |

## Status

What each team's evaluation deployment supports, and what the other team has checked.

- ⬜ **Not yet:** not deployed.
- 🟡 **Live:** deployed, and the team's own tests pass against it.
- ✅ **Verified:** the *other* team's suite passed against it. Add the date.

Each team updates its own column. A ✅ is added by the other team, after their suite
passes, so no team marks its own work verified.

### Operations

| Operation | Phase | Team 17 | Team 21 |
|---|---|---|---|
| `GET /health` | 3 | 🟡 Live | ⬜ |
| `POST /tickets` | 3 | 🟡 Live | ⬜ |
| `GET /tickets/{id}`, `PATCH /tickets/{id}` | 3 | 🟡 Live | ⬜ |
| `POST /tickets/{id}/triage` | 3 | 🟡 Live | ⬜ |
| `GET /tickets/{id}/audit` | 3 | 🟡 Live | ⬜ |
| `POST /test/reset`, `GET`/`PUT`/`DELETE /test/clock` | 3 | 🟡 Live | ⬜ |
| `GET /tickets` with `state` and `obligation_status` filters | 4 | 🟡 Live without filters (filters are ignored) | ⬜ |
| `GET /tickets/{id}/reports/{type}` (drafts) | 4 | ⬜ | ⬜ |
| `POST /tickets/{id}/reports/{type}/submission` | 4 | ⬜ | ⬜ |
| `POST /tickets/{id}/close` | 4 | ⬜ | ⬜ |

### Milestones

Mark each one "Done" with the date. The "other team's suite" row is filled in by the
other team.

| Milestone | Team 17 | Team 21 |
|---|---|---|
| Base URL posted here | Done, 2026-10-18 | ⬜ |
| Token sent to the other team | Done, 2026-10-18 | ⬜ |
| Own Phase 3 suite passes against own deployment | Done, 55/55, 2026-10-18 | ⬜ |
| Other team's Phase 3 suite passes against this deployment | ⬜ | ⬜ |
| Phase 3 operations frozen in the shared contract (both teams) | ⬜ | ⬜ |

### Team 17's deployment

- System documentation (as delivered): [`team17/SYSTEM.md`](team17/SYSTEM.md)
- How to test it, and how it behaves where the contract leaves room for interpretation:
  [`team17/TESTING.md`](team17/TESTING.md)
- Automated tester (runs against any team's deployment): [`team17/tester/`](team17/tester/README.md)

Team 21 can add matching files under `team21/`.

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
| Shared contract, `openapi.yaml` | Phase 2, Section 5 | Published here (draft). Next: both teams freeze the Phase 3 operations in this copy. |
| Shared test file: inputs and hand-calculated outputs | Phase 2, Q4.2 | Starts with the Model D deadline examples. Each expected output is calculated by one team and checked by the other before it's added. |

## Changing this repo

Coding agents (either team's) follow [`AGENTS.md`](AGENTS.md).

- All changes arrive as pull requests.
- **Any change to the shared contract or the shared test file needs approval from someone
  on the other team** before it merges.
- Each team may update its own row in the deployments table, its own deployment section,
  and its own column in [Status](#status) without the other team's approval. The other
  team adds the ✅.
