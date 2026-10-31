# CLAUDE.md

A single-operator Python 3.12 agent. It runs daily on a Raspberry Pi in Docker, collects news for one or more newsletters into Postgres, has Claude score and summarize it through `claude-agent-sdk`, and emails a digest per newsletter through Gmail SMTP.

**[ARCHITECTURE.md](ARCHITECTURE.md) is the source of truth** for requirements (REQ-xx), decisions, research findings, risks and the design. Read it before changing behavior. When a change alters anything it describes, update it in the same change. Setup and operations are in [README.md](README.md).

## Hard rules

- **No comments in code or config files.** That means no `#` comments, no `noqa` or `type: ignore` markers, and no comments in TOML, SQL, the Dockerfile, compose or systemd files. The owner's reason: comments go stale and mislead. When code needs explaining, rename or restructure it instead.
  - **Docstrings are allowed**, because they're standard in Python. Keep them short. Give them to modules, and to public functions whose name and signature don't already show what they return, raise or guarantee. Update a docstring in the same change as the code it describes.
  - Markdown documents are fine.
- **No unit or integration tests.** Don't add a `tests/` folder, pytest, or test-only hooks in the code. Verify a change by running the real program, as in "Verify a change" below, and report what ran.
- **The standard library first.** The app is low-throughput and personal, but it must be reliable. Add a third-party package only when the standard library has no workable equivalent. There are four runtime dependencies, each for a reason:
  - `claude-agent-sdk`, pinned exactly: it's the required way to call Claude;
  - `psycopg`: the standard library has no Postgres driver;
  - `youtube-transcript-api`: it reads transcripts from an unofficial YouTube endpoint;
  - `requests`: that library needs it, and it is used to put a timeout on the library.

  HTTP, feeds, sitemaps, HTML text, email and config all use the standard library.
- **Never commit or push** unless the owner asks.
- **Every change starts with an issue and reaches `main` only through a pull request** (REQ-35, REQ-42). No exceptions, however small the change, docs and config included:
  1. Create an issue that describes the change, before any commit.
  2. Create a branch from the latest `main`.
  3. Commit on the branch and push the branch.
  4. Open a pull request with `Closes #<issue>` in its description. The PR hygiene job fails without a linked issue (REQ-34).
  5. Merge only after every check passes, Claude review included: Claude must post no findings (REQ-44, REQ-45).

  **Never push to `main` directly.** The rule on `main` blocks it, and blocks a merge while any check is red (REQ-50).

  **Never add the `review-override` label.** It passes Claude review without Claude's approval, and only the owner decides that. It counts only for commits pushed before it was added, so a later push is reviewed again. Never re-run Claude review or the Workflow guard yourself.
- **The pipeline enforces these rules** (`.github/workflows/`, ARCHITECTURE.md section 12) with 17 checks, from no comments and no tests to strict types, dead code, the secret scan, the vulnerability audit and Claude review. Git hooks run most of them locally (REQ-47): the fast checks on every commit, and Types, Duplicates, Vulnerabilities and Secrets on every push. Never bypass them with `--no-verify`; fix what they report.
  - A new kind of file fails the comment check until `scripts/check_no_comments.py` gets a check for it.
  - The Workflow guard fails a pull request that changes `.github/`, `scripts/`, `.gitleaks.toml`, `.squawk.toml` or `.pre-commit-config.yaml` until the owner adds `review-override`, because those files define the checks.
  - Ruff bans each boundary's import outside its owning module (the table below). Don't add an exemption to `pyproject.toml` to get around it; move the code into the owner.
  - With no `noqa` and no `type: ignore`, fix the code instead. Where a rule truly doesn't apply, exempt it per file in `pyproject.toml`, and give the reason in ARCHITECTURE.md section 12.
  - Don't raise the complexity limit (19); split the function.
  - Pin every action by commit, every image by digest, and every downloaded tool by version and checksum.

## Commands

```bash
uv sync
uv run pre-commit install
uv run pre-commit run --all-files
uv run pre-commit run --all-files --hook-stage pre-push
uv audit --locked --preview-features audit
uv run python -m newsletter_agent --help
```

Running the agent needs Postgres and the secrets. See README.md, "Development", for a local Docker setup with `NEWSLETTER_AGENT_ENV_DIR` pointing to a folder **outside** the repository.

## Verify a change

Run the code path you changed against real services, with the cheapest settings: `SCORE_MODEL=claude-haiku-5-5`, `DIGEST_MODEL=claude-haiku-5-5`, `SEARCH_MODEL=claude-haiku-5-5` and a small `MAX_ITEMS_PER_RUN`.

- `check` covers the configuration, the database, SMTP, the YouTube key, one Claude call, and one web search when a newsletter has a `web_search` source.
- `run` against a local database covers the pipeline. Inspect the `runs`, `items`, `assessments` and `digests` tables afterward.
- For failure paths, break one setting on purpose: an invalid `CLAUDE_CODE_OAUTH_TOKEN`, an unreachable `DATABASE_URL`, or fake SMTP credentials. Then check the run's status and exit code. A throwaway script outside the repository is acceptable only where the real program can't show the behavior.

## How the code is laid out

Each external boundary has one module:

| Boundary | Module | Constraints |
|---|---|---|
| Claude | `llm.py` | The only module that imports `claude_agent_sdk` |
| Postgres | `store.py` | The only module that imports `psycopg`; all SQL lives here. Other modules type connections as `store.Connection` |
| HTTP | `http.py` | Every outgoing request goes through it, except YouTube transcripts, which `youtube-transcript-api` fetches with its own `requests` session and a 30-second timeout |
| Gmail | `mail.py` | SMTP to send; IMAP only to read rating emails from the Sent folder: read-only (`EXAMINE`), headers only, the rating subject only. Pass `ssl.create_default_context()` to both, because `smtplib` and `imaplib` don't check certificates without it |

The rest of the package:

- `run.py` runs the steps in `steps.py` in order: read ratings, collect, enrich, assess, digest, deliver.
- `sources/` holds one collector per source type, registered in `COLLECTORS`.
- Prompts live in `src/newsletter_agent/prompts/` and are filled with `prompts.render(name, **values)`, which fills every placeholder in one pass, so untrusted text filled into one placeholder is never searched for another. Don't chain `.replace` calls on a template. Migrations live in `src/newsletter_agent/migrations/`. Both ship inside the package.
- **Configuration is per newsletter.** Each newsletter is a folder, `config/newsletters/<key>/`, holding `newsletter.toml` (its `name`, an optional `score_threshold`, and its `[[sources]]`) and `interests.md`.
  - Recipient addresses come only from `DIGEST_TO_<KEY>` in the secrets file. Never put an address in `config/`.
  - The `ai` newsletter is the owner's. Only newsletters are generalized, not other kinds of agents.

## Things that must stay true

- **Claude gets no tools, except `WebSearch` in the web search step.** The owner made that exception as a product decision (REQ-29).
  - `llm.structured` passes `tools=[]` and fails closed unless the session offers exactly `["StructuredOutput"]`. Only `sources/web_search.py` passes `web_search=True`, which allows `WebSearch` and nothing else, and the check then expects exactly `StructuredOutput` and `WebSearch`. Don't relax either check or add another tool; a new capability must be written as code, unless the owner decides otherwise.
  - Links that Claude's search returns are untrusted. `web_search._page_url` keeps only valid `http` or `https` URLs, at most 2,000 characters, without credentials, on port 443 once turned into https, whose host resolves to public addresses.
  - Fetched text is untrusted. It goes into prompts inside `<item>` tags, through `text.fence`.
  - Links in emails come only from stored item URLs, never from Claude's text. The one exception is the pair of `mailto:` rating links that `mail.feedback_link` builds from `SMTP_USERNAME`, the item's ID and the database's feedback token, only in digests addressed to `SMTP_USERNAME`. `mail.render_digest` and `mail.render_failure` pass every text they show (titles, summaries, score reasons, the intro, error text in the footer and the failure reason) through `text.defang`, and every value in the HTML is escaped. Don't defang elsewhere, and don't add a shown value that skips it.
  - Titles of rated items are untrusted too. They go into the scoring prompt inside `<feedback>`, through `text.fence`, which escapes both `item` and `feedback` tags.
- **`settings.take_secrets` removes secrets from `os.environ` before any Claude call,** because the SDK hands the whole environment to Claude Code. Only `CLAUDE_CODE_OAUTH_TOKEN` stays. Add any new secret to `SECRET_VARS`.
- **`http.py` fetches over https only and refuses non-public addresses** (loopback, private, link-local, CGNAT, Docker networks, and their IPv4-mapped and NAT64 forms) on every request and every redirect, and connects only to the addresses it checked, so DNS rebinding can't redirect it. It allows no port but 443: the owner accepted fetching any public domain (REQ-57), not any port. Keep that check and the pinned connection, the upgrade of http to https, its socket timeout, its 90-second deadline over the whole request, its size cap, and its dropping of the caller's headers on a cross-host redirect. The deadline uses `SIGALRM`, so call `http.get` and `http.deadline` only from the main thread, and never one inside the other.
- **Every text written to Postgres is made storable** by `text.storable`, which drops NUL characters and broken Unicode: `store._safe` applies it to every text column, and `llm.structured` to Claude's whole output, which also reaches `jsonb`. Postgres refuses either, and a refused write of an error message once failed every later run.
- **Claude review only reviews** (REQ-63). Its job never changes a file, commits or pushes; changes come from the owner's own Claude Code session. Keep the settings in `.github/workflows/claude-code-review.yml` that make it so:
  - the job's only permissions are `actions: read`, `contents: read` and `pull-requests: write`;
  - `github_token: ${{ github.token }}`, and no `id-token: write`: without them the action takes the Claude GitHub app's token, which can push;
  - `--allowedTools` is exactly the three `gh pr` commands and the inline-comment tool, so Claude can't run git or anything else, and `--disallowedTools` denies Edit, Write, MultiEdit and NotebookEdit;
  - no `use_commit_signing`, `ssh_signing_key` or `mcp__github` tool, each of which would give it a way to commit, and no `--permission-mode`, `--dangerously-skip-permissions`, `settings`, `plugins`, `--mcp-config` or `additional_permissions`, each of which would widen what it may do.

  `scripts/check_review_workflow.py` checks these, in the Workflows job and a commit hook, and fails when a permission or a tool list appears twice or spelled another way. Every problem the review finds blocks the merge (REQ-64): don't loosen its prompt to let minor ones through. A pull request is reviewed by its own copy of the workflow, so the rule and the check catch mistakes, not a deliberate rewrite; the owner reads changes under `.github/`, which the Workflow guard enforces.
- **The database connection uses TLS 1.3, as its own non-superuser role.** `store.connect` always uses `sslmode=verify-full`, whatever `DATABASE_URL` says, and refuses an unencrypted connection; `db/init-agent.sql` creates the role on Postgres's first start; `db/pg_hba.conf` refuses any TCP connection without TLS, and any TCP login but the agent's role into its own database. The setup must stay one that other users of the project can follow without trouble (REQ-58).
- **Every I/O call has a timeout,** and a run has a deadline, a `time.monotonic()` value. A collector that makes several requests checks `sources.base.past(deadline)` before each one after the first, and raises `DeadlinePassed`, so the source fails instead of recording a success for work it skipped. A watchdog in `run.py` ends the process 15 minutes after the deadline, so a hang can't hold the run lock. Keep `RUN_DEADLINE_MINUTES` plus 15 minutes under the unit's `TimeoutStartSec=2h`.
- **Caps are per run.** `MAX_ITEMS_PER_RUN` limits fetching, and separately scoring, across all newsletters, shared by `steps.shares` (REQ-17). Within a newsletter, `store.items_to_enrich` and `store.items_to_assess` take one source at a time, from its configured, enabled sources only, so a busy source can't crowd out the others.
- **SMTP error text never reaches a log or the database.** It quotes email addresses; log and store `mail.error_summary(e)` instead.
- **One bad entry fails only its source.** `steps._collect_source` skips a candidate with an invalid or over-long URL, and turns any error while storing a source's items into a failure of that source, never of the run.
- **Runs must be safe to repeat.** Inserts use `on conflict do nothing`, a digest is keyed by its run and newsletter, and an advisory lock stops overlapping runs.
- **Never edit a migration that any database has applied.** Every schema change, adding or dropping a column included, is a new file in `src/newsletter_agent/migrations/`, numbered one more than the highest number there. `migrate()` applies it at the start of the next run, in one transaction. Dropping a column destroys its data, and there are no backups.
- **Owner-only information stays with the owner.** Failure emails go to `SMTP_USERNAME`. The health footer, the token warning and the rating links appear only on digests addressed to `SMTP_USERNAME`, and only ratings of items from those digests are stored. Another newsletter's recipient must never see source errors, run counts or setup instructions.
- **Search caps are shared.** Google's quota belongs to the API key, so all newsletters' YouTube search terms together must fit under `YOUTUBE_SEARCH_MAX_QUERIES`. Web searches all use the owner's Claude plan, so all web search terms together must fit under `WEB_SEARCH_MAX_QUERIES`.
- **Auth failures must be classified as auth failures.**
  - An invalid token yields a `ResultMessage` with `subtype="success"` and `is_error=True`, so check `is_error` first.
  - Errors that can't be classified become `ClaudeAuthError`, which is the loud path.
- **Exit codes:** 0 for a run that succeeded or partly succeeded; 2 for a failed run whose failure email went out; 1 for a failed run whose email didn't, or a run the watchdog stopped. The systemd unit relies on these.

## The Raspberry Pi

**Other services the owner depends on run on the Pi.** Never touch another project on it, never publish ports from this one, and never use host networking, `privileged` or device mounts.

- **This repository is public.** The Pi's private details and the owner's own rules for it are in `CLAUDE.local.md`, which is gitignored (`*.local.*`) and loads beside this file when it exists. Never copy anything from it, or the Pi's hostname, user, password, ports or network details, or anyone's personal details or email address, into a tracked file, an issue, a pull request or a comment. History is permanent.
- Access instructions are in the owner's knowledge base, outside this repository.
- On the Pi, this project runs from `/opt/newsletter-agent`, owned by root, installed from the staging folder `~/Projects/newsletter-agent` (README, "Deploy on the Pi").
- Deploy only a reviewed `main` commit (REQ-60): `git archive origin/main` into the staging folder, with the commit in `REVISION` for the build. Never copy the Mac's working tree or another branch to the Pi.
- Secrets are in `/etc/newsletter-agent/agent.env` and `db.env`, owned by root with mode `0600`. Never put secrets in the repository or in chat.
- Deploying, enabling the timer, or sending real email needs the owner's go-ahead each time.
- The Pi kernel ignores container memory limits (`cgroup_disable=memory`). CPU limits work.
- The Pi's shell is bash, but the Mac's is zsh. In zsh, `$VAR:x` applies a modifier, so `"$H:Projects"` doesn't mean what it looks like. Write `"${H}:Projects"`.

## Docs

- ARCHITECTURE.md is the single source of truth. New requirements, decisions, findings and risks go there, not into new documents. There is no PRD; it was folded into ARCHITECTURE.md.
- README.md covers setup and operations only, apart from the owner's banner at its top (REQ-67), and must agree with ARCHITECTURE.md. The banner states no merge, approval or check result, because the file can't know them.
- Keep both in step with the code.
