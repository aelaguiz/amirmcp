# Agent Notes

amirmcp is Amir's connector that lets his OpenAI dot, which runs on OpenAI's
servers, see the Claude, Codex and agy coding sessions running in his Herdr spaces on
this Mac. Start with [README.md](README.md). Requirements, implementation plan
and test plan live in [docs/](docs/). `CLAUDE.md` is a symlink to this file, not
a second rulebook.

These notes carry over the parts of Amir's working rules from
`/Users/aelaguiz/workspace/psagentspace/AGENTS.md` that apply to this repo. When
the two disagree, that file is the newer source of his preferences.

## Teach intuitions, not heuristics

This comes first whenever we write a prompt, brief or talk to an agent, write a
skill, or carry a lesson forward in this repo (Amir, 2026-10-01 and 2026-10-04).
The summarization prompts this connector sends to Haiku are model-facing
prompts too.

Agents are smart, and they do their best work when they understand what we're
after and why. So teach the way you'd brief a sharp new colleague: say in plain
English how to think about the thing, why it matters to the reader, and show a
few cases that look different from one another. Then trust them with the next
case, including one nobody has seen yet.

If-thens, keyword lists, invented thresholds, checklists and harnesses feel
safer, but they're brittle. Each one describes the last case, so the next case
in different clothes slips past it. They teach an agent to satisfy the check
instead of looking at the problem. So when you feel the pull to add a rule, take
it as a sign that something wasn't understood: work out what, and teach that.

Code and mechanical checks have a real place: facts you can look up rather than
weigh, such as a schema, a file on disk, a process table or a pane id. Use them
for what they're good at, not to cover a judgment an agent got wrong. When two
careful experts could disagree about a case, it's a judgment, and it belongs in
an intuition. The full practice is in `$skill-authoring`.

## Work and verification

- Implement only the requested outcome, using the smallest coherent change.
  No speculative modes, flags, wrappers, fallback layers, or validation systems.
  A normal targeted test is the default; validate real boundaries and fail loud.
- Before executing a plan, ask once where it overbuilds and cut that work.
  Use `$startup-pragmatism` during planning. Never use engineering-time estimates
  to scope, prioritize, or recommend work.
- The capability brief this repo answers to asked for an on-demand first
  version and said not to build elaborate security: one shared secret on the
  public endpoint. On 2026-10-10 Amir asked for the dot to send messages to
  agents and answer their prompts (docs/SENDING_TO_AGENTS.md); starting,
  stopping or restarting agents is still out until he asks.
- Use `rg` first, scoped to the relevant path. Never run `rg`, `find`, or similar
  broad scans across home, parent directories, `/`, or other large trees unless
  Amir explicitly asks; they disrupt his parallel work. Read the session homes
  this code already knows about instead.
- Run checks that cover the change, inspect the diff, and report what passed
  or remains unverified. "Works" means the real dot answered from real sessions,
  not that unit tests passed.

## Authorization and shared work

Most calls are ours to make from Amir's goals. A big decision is his: one that
takes away something he relies on, spends real money, or would be hard to walk
back. It needs him to say yes to that specific thing in his own words; silence
or a passing "sure" is not a decision.

Amir's ask authorizes ordinary in-scope reads, local edits, routine login, SSO
and OAuth consent, consent/license screens, and infrastructure repair. Complete
those steps yourself with the available credentials and the BrowserOS Work
profile. Do not turn ordinary work into an approval checklist. The only access
blockers are a physical unlock, biometric, 2FA code or password that only Amir
holds; finish everything else first.

- A task Amir gave that delivers a commit, branch, or PR already authorizes the
  commits and pushes that deliver it; do not ask again. Stage exact paths, never
  `git add .` or `git add -A`. Commit only source and durable docs, not caches,
  builds, logs or temp output.
- Force pushes, history rewrites, branch/tag deletion and destructive Git
  cleanup require an exact ask.

## Credentials

Never print or commit secret values. `.env` holds the live Anthropic key and the
connector's shared secret and is gitignored. Never rotate or invalidate exposed
or suspected-exposed credentials; report exposure without repeating the secret.
Revocation or deletion requires Amir's approval for the exact credential and
action.

## Clean up what you start

Amir works on this machine while agents run. Ask him before starting local
model training, local model serving, or any other long, heavy CPU or GPU job.

Run wedge-prone work as a watched background task with an expected runtime and
a stall action. On completion, failure or a change of approach, stop task-owned
jobs and verify their descendants and listeners exited. Never kill by process
name alone. The connector service and its tunnel are intentional survivors:
they run under launchd (see README) and are stopped with `launchctl`, nothing
else.

## Agents, skills and browser

Use `$prompt-authoring` before every model-facing prompt, including follow-ups,
and keep briefs small. Use a skill for the workflow it actually covers, not
because one word overlaps.

Browser work: read and apply `browseros` before the first call. Work in
background tabs of existing windows without taking foreground focus, and open
login, SSO and OAuth pages yourself in the Work profile. The dot is set up and
tested in the BrowserOS Work profile. If BrowserOS is down, repair it.

## Communication

Lead with the concrete outcome and name what changed. Use plain English and full
absolute file paths; no invented named personas. Never refer to anything only by
its number or code: say the number with a brief plain description, linked when
it has a link, e.g. commit eb8816e656 (took the switch-off out of the push
check). Working docs stay Markdown in this repo; build a rendering only when
Amir asks to visualize. No em dashes or "receipt(s)"; use "evidence" or
"source."
