# Team 17's tester

Team 17's automated black-box tests for the pod's common interface. They run over HTTPS
against **any** team's deployment, needing only its base URL and an access token, and
know nothing about how that service is built.

**Covers (Phase 3):** AC-01 to AC-05, AC-14, AC-15, and AC-17 from the pod spec, plus the
Model D E6 boundary. Every response is also checked against the response schemas in the
pod's contract (`openapi.yaml`, included here).

This folder is a published copy of the runner in Team 17's (private) service repository,
refreshed at each Team 17 release.

## Warning: it resets the target

Every test starts with `POST /test/reset`, which deletes **all** data on the target
deployment, for every client. Run it only against a deployment you're allowed to reset, at
an agreed time, and never two runs at once. It refuses to start without `--allow-reset`.

## Run it

Needs Python 3.12 or later. No database or web framework is needed.

```bash
git clone https://github.com/team17-ece30861/pod-k-shared.git
cd pod-k-shared/team17/tester

python3 -m venv .venv
source .venv/bin/activate
pip install pytest PyYAML "jsonschema[format-nongpl]"

export CRA_BASE_URL=https://<system-base-url>
export CRA_API_TOKEN=<token>       # issued to you privately; never commit or share it
python -m pytest -c acceptance/pytest.ini acceptance --allow-reset -v
```

| Option | Meaning |
|---|---|
| `--base-url URL` | Target system. Defaults to `CRA_BASE_URL`. Must be HTTPS, except for `localhost`. |
| `--allow-reset` | Required. Confirms you may reset the target. |
| `--report FILE` | Optional JSON report of run times, target, counts, and each test's outcome. Never contains the token or request bodies. |

## Results and run time

Against an HTTPS deployment that implements the Phase 3 operations correctly, all 55 tests
pass. A full run takes about 80 seconds against Team 17's deployment. Each request times out
after 30 seconds, and a service that scales to zero may take several seconds to answer its
first request.

To leave the target clean afterward:

```bash
curl -X POST   "$CRA_BASE_URL/test/reset" -H "Authorization: Bearer $CRA_API_TOKEN"
curl -X DELETE "$CRA_BASE_URL/test/clock" -H "Authorization: Bearer $CRA_API_TOKEN"
```
