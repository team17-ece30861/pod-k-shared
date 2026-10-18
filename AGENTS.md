# Agent instructions for pod-k-shared

This repo is shared by **Team 17** and **Team 21** (ECE 30861, Pod K). These rules apply to
every coding agent working here (Claude Code, Codex, or any other), whichever team it
works for. They cover only this shared repo. Each team's own repo keeps its own workflow.

Before changing anything, read [`README.md`](README.md), and know which team the person
driving you is on. If you can't tell, ask.

## Rules

1. **No secrets, ever.** Never put a token, password, or other secret in a file, commit,
   branch name, issue, PR, or comment, even while debugging a 401. Tokens are exchanged
   privately between teams.
2. **Stay in your own team's lane.** Edit only your own team's row in the deployments
   table, its own deployment section, and its own column in the status chart. Never
   change the other team's entries, even to fix something that looks wrong. Tell your
   person instead.
3. **Never verify your own team.** In the status chart, only the other team marks an
   operation ✅ verified, after their suite has passed against it.
4. **Contract and test-file changes need the other team.** Any change to the shared
   contract (`openapi.yaml`) or the shared test file goes through a pull request, and
   someone on the other team approves it before it merges. Agents never merge these.
5. **Expected outputs are calculated by hand.** Expected values in the shared test file
   come from the pod spec and are worked out by hand, then checked by the other team.
   Never generate them by running either team's implementation.
6. **Read the team's testing guide first.** Before testing a team's deployment, read that
   team's `TESTING.md` (for example [`team17/TESTING.md`](team17/TESTING.md)), including its
   section for coding agents.
7. **Don't test the other team without a yes.** Running a suite against the other team's
   deployment resets all its data. Do it only when your person explicitly says so, and
   afterward leave it clean: `POST /test/reset`, then `DELETE /test/clock`.
8. **Disagreements go to people.** If the contract, the test file, or the other team's
   results look wrong, describe the problem for your person to raise with the other
   team. Don't change shared material to make your own team's results pass.
