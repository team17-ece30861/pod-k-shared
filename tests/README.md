# Shared test file

The pod's shared test cases: inputs and hand-calculated expected outputs that both teams'
deployments must reproduce exactly (Phase 2, Q4.2 and Q6.1). Phase 3 covers the Model D
deadline examples, E1 to E8, in [`model-d.json`](model-d.json).

## Rules

- **Expected values are calculated by hand from the pod spec**, never by running either
  team's implementation.
- **One team calculates, the other checks.** Each case records `calculated_by` and
  `checked_by`. A case is final only when both are filled in, by different teams.
- **Changes need the other team's approval** before they merge (see the repo's
  [`AGENTS.md`](../AGENTS.md)).

## Format

Each case lists `steps` to run in order after `POST /test/reset`: `set_clock`,
`submit_ticket`, `triage`, `update`, and `get_ticket`. A step's `expect` gives the status
code and the response fields to check. Only the listed fields are compared, field by
field. See `conventions` at the top of the file.

## Running it

Any suite can read the file. `run.py` is a small runner that needs only the Python standard
library:

```bash
export CRA_API_TOKEN=...   # the token the other team sent you; never commit it
python3 tests/run.py tests/model-d.json https://<base-url>
```

It resets the deployment before each case and leaves it clean (reset, then real time)
when it finishes, so run it only when you're allowed to reset that deployment, and never
in parallel.
