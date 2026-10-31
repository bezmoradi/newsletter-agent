<h1 align="center">⚙️ Engineered, Not Vibe Coded</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Engineered-not%20vibe%20coded-blue?style=for-the-badge" alt="Engineered, not vibe coded">
</p>

## #1 Stop vibe coding <img src="https://img.shields.io/badge/Merged-8957e5?style=flat-square&logo=git&logoColor=white" alt="Merged">

```diff
- vibe coded: shipped because it compiled
+ engineered: designed, reviewed, documented
```

<sub>✅ Approved by human reviewers · All checks passed</sub>

# Newsletter agent

A personal agent that runs once a day on a Raspberry Pi. It runs one or more **newsletters**. The first is AI news: blogs, the Anthropic and OpenAI sites, YouTube channels, a YouTube topic search, and a web search that Claude runs with its own search tool. Each newsletter has its own sources, interest profile and recipient. The agent stores everything in Postgres and has Claude score each item against its newsletter's profile. When something scores high enough, it emails that newsletter's recipient a short digest from your Gmail account.

- A newsletter with nothing new sends no email.
- A run that fails sends a short failure email, to you only.
- Only digests addressed to you carry the health footer: run counts, failing sources, your ratings and the Claude token countdown.
- Only digests addressed to you have "More like this" and "Less like this" links on each item. Each run reads the emails those links create from your Gmail Sent folder, and scoring uses your recent ratings as examples of your taste. See "Rating items" below.
- Claude reads text and returns JSON. It gets no tools, except its web search tool in the web search step. [ARCHITECTURE.md](ARCHITECTURE.md) is the source of truth for requirements, decisions and design.

## Table of Contents

- [What you need](#what-you-need)
- [Configuration](#configuration)
- [Deploy on the Pi](#deploy-on-the-pi)
- [Running it](#running-it)
- [Maintenance](#maintenance)
- [Development](#development)
- [Author](#author)
- [License](#license)

## What you need

- A Raspberry Pi with Docker and Docker Compose. This setup was checked on a Pi 5 with Docker 29 and Compose 5.4.
- A Claude subscription token from `claude setup-token`. It is valid for one year.
- A Gmail app password, made as below. A free Gmail account works.
- A YouTube Data API key, made as below, if a newsletter has a `youtube_search` source.

### Gmail app password

The agent signs in to Gmail with an app password. It sends every email through `smtp.gmail.com`, and reads your rating emails from the Sent folder over IMAP.

1. Turn on 2-Step Verification for the account, under **Security** at https://myaccount.google.com/security. Google offers app passwords only once it's on.
2. Open https://myaccount.google.com/apppasswords, signed in as that account. Name the password, such as `newsletter-agent`, and click **Create**.
3. Copy the 16-letter password Google shows; it isn't shown again. In `agent.env`, set `SMTP_APP_PASSWORD` to it without the spaces, and `SMTP_USERNAME` to the account's address.

`check` then reports `ok smtp` and `ok imap`.

If the page offers no app password, Google gives these reasons: 2-Step Verification is off or uses only security keys, the account belongs to a work, school or other organization, or it has Advanced Protection. Google's help: [Sign in with app passwords](https://support.google.com/accounts/answer/185833).

### YouTube Data API key

YouTube topic search needs a key for the YouTube Data API v3. Google's daily quota is 10,000 units, and each search term costs 100 units a run.

1. Create a Google Cloud project at https://console.cloud.google.com/projectcreate, such as `newsletter-agent`, and select it.
2. Enable the API at https://console.cloud.google.com/apis/library/youtube.googleapis.com.
3. At https://console.cloud.google.com/apis/credentials, click **Create credentials**, then **API key**. If Google asks what data you'll access, choose **Public data**.
4. Open the key, set **API restrictions** to **Restrict key** with only **YouTube Data API v3** ticked, and click **Save**. Leave **Application restrictions** at **None**, because your home IP address can change.
5. In `agent.env`, set `YOUTUBE_API_KEY` to the key.

`check` then reports `ok youtube: the API key works`.

## Configuration

Each newsletter is a folder under `config/newsletters/`. The folder's name is the newsletter's key: lowercase letters, digits and dashes, such as `ai` or `cooking`. Each folder holds two files.

**`newsletter.toml`** has:

- `name`, shown in the email's subject and heading, such as `'AI news'`;
- an optional `score_threshold` from 0 to 10, which overrides `SCORE_THRESHOLD` for this newsletter;
- one `[[sources]]` entry per source.

Each source has:

- a `key`, made of lowercase letters, digits and dashes;
- a `name` shown in the digest;
- a `type`, which decides the other fields:

| `type` | Fields |
|---|---|
| `feed` | `url` of an RSS or Atom feed, and an optional `include`, a list of regular expressions for the URL paths to keep |
| `sitemap` | `url` of a sitemap, and `include`, a list of regular expressions for the URL paths to keep |
| `youtube` | `channel_id` |
| `youtube_search` | `queries`, the search terms. All newsletters' terms together can't exceed `YOUTUBE_SEARCH_MAX_QUERIES`, because Google's daily search quota belongs to your API key. |
| `web_search` | `queries`, the search terms. Claude searches the web for each term with its `WebSearch` tool, its only tool, and returns recent links; no other key is needed. All newsletters' terms together can't exceed `WEB_SEARCH_MAX_QUERIES`, because every search uses your Claude plan. |

Only `feed` and `sitemap` sources take `include`; on any other type the configuration is refused. Add `enabled = false` to an entry to skip it. The first time a source is seen, its existing entries are stored as a baseline and not scored. The exceptions are feed items published within the last `BACKFILL_DAYS`, and web search results, which are always recent.

**`interests.md`** is the newsletter's interest profile, in plain prose. Claude scores the newsletter's items against it.

**The recipient** comes from `DIGEST_TO_<KEY>` in the secrets file, with the key uppercased and dashes turned into underscores: `DIGEST_TO_AI`, `DIGEST_TO_COOKING`. Without it, the newsletter goes to `SMTP_USERNAME`, which is you. Addresses never go in the repository.

**To add a newsletter:**

1. Copy `config/newsletters/ai/` to a new folder.
2. Edit its `name`, sources and `interests.md`.
3. Add `DIGEST_TO_<KEY>` to the secrets file.
4. Run `check`. Its sources start with a baseline on the next run, like any new source.

**Environment variables.** The agent reads these at the start of every run, so changing a value needs no rebuild.

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | required | The token from `claude setup-token` |
| `DATABASE_URL` | required | `postgresql://newsletter_agent:<AGENT_DB_PASSWORD>@db:5432/newsletter_agent?sslmode=verify-full&sslrootcert=/run/db-tls/server.crt` |
| `SMTP_USERNAME` | required | Your Gmail address. Every email is sent from it, and failure emails go to it |
| `SMTP_APP_PASSWORD` | required | The Gmail app password, without the spaces; see [Gmail app password](#gmail-app-password) |
| `DIGEST_TO_<KEY>` | `SMTP_USERNAME` | Where newsletter `<key>`'s digest goes |
| `YOUTUBE_API_KEY` | required with a `youtube_search` source | The YouTube Data API key; see [YouTube Data API key](#youtube-data-api-key) |
| `SCORE_MODEL`, `DIGEST_MODEL`, `SEARCH_MODEL` | `claude-opus-5-5` | The Claude model for scoring, the digest and web search |
| `SCORE_THRESHOLD` | `6` | The lowest score, out of 10, that goes in a digest, unless a newsletter sets its own |
| `MAX_ITEMS_PER_RUN` | `100` | The most items fetched, and the most scored, in one run across all newsletters, shared evenly between them. Within a newsletter, items are taken one source at a time, each source's newest first, so a busy source can't crowd out the others. The rest wait for the next run, and your footer shows how many are waiting |
| `DIGEST_MAX_ITEMS` | `15` | The most items in one email |
| `SCORE_BATCH_SIZE` | `20` | Items per scoring call |
| `BACKFILL_DAYS` | `7` | How far back a new feed's items count as new |
| `YOUTUBE_SEARCH_MAX_QUERIES` | `10` | The most search terms across all newsletters, which is one search call each per run; the API allows 100 a day |
| `WEB_SEARCH_MAX_QUERIES` | `10` | The most web search terms across all newsletters, at most 50. Each is one Claude session per run, with up to 3 searches |
| `TRANSCRIPTS_ENABLED` | `true` | Set `false` to stop fetching YouTube transcripts |
| `TOKEN_CREATED` | `2026-10-30` | When the Claude token was made; used for the expiry warning |
| `RUN_DEADLINE_MINUTES` | `90` | A run stops starting new work after this long, and the process exits 15 minutes later if it's still going. It can be at most 104, so that happens before the unit's 2-hour limit |

The settings code removes every secret except `CLAUDE_CODE_OAUTH_TOKEN` from the environment before Claude Code starts.

## Deploy on the Pi

The code arrives in a staging folder, `~/Projects/newsletter-agent`, and runs from `/opt/newsletter-agent`, which root owns. The units run as root, so they use only files your login account can't change.

1. **Copy the latest reviewed commit on `main`** from your Mac to the staging folder, running this in the repository:

   ```bash
   git fetch origin
   git archive origin/main | ssh <user>@<pi-host> \
     'rm -rf ~/Projects/newsletter-agent && mkdir -p ~/Projects/newsletter-agent && tar -x -C ~/Projects/newsletter-agent'
   git rev-parse origin/main | ssh <user>@<pi-host> 'cat > ~/Projects/newsletter-agent/REVISION'
   ```

   The copy holds exactly the files of that commit, whatever branch is checked out or changed on your Mac: no `.venv`, caches, secrets or `*.local` notes. Deploys come only from `main`, which passed every check. `REVISION` records the commit: the image is built with it, and `check`, each run's first log line and every failure email show it.

   Then, on the Pi, install it:

   ```bash
   sudo rsync -a --delete --chown=root:root ~/Projects/newsletter-agent/ /opt/newsletter-agent/
   ```

   **Warning:** the first command empties the staging folder before copying, and this one uses `--delete`, which removes anything in `/opt/newsletter-agent` that isn't in the copy. Keep nothing else in either folder; the secrets live in `/etc/newsletter-agent`. Run both to deploy a new version, then rebuild with step 3.

2. **Create the secrets and the database certificate** on the Pi. The secrets files are owned by root, readable only by root, and kept outside the repository. Postgres has two passwords: `POSTGRES_PASSWORD` for its admin account, and `AGENT_DB_PASSWORD` for the agent's own account, which owns only the agent's database. The first command generates both as random hex, which needs no escaping in `DATABASE_URL`, and never shows them on screen.

   ```bash
   sudo install -d -m 700 /etc/newsletter-agent
   sudo sh -c 'umask 077 && printf "POSTGRES_PASSWORD=%s\nAGENT_DB_PASSWORD=%s\n" "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > /etc/newsletter-agent/db.env'
   sudoedit /etc/newsletter-agent/agent.env   # the variables above; DATABASE_URL uses AGENT_DB_PASSWORD from db.env
   sudo chmod 600 /etc/newsletter-agent/db.env /etc/newsletter-agent/agent.env
   sudo install -d -m 755 /etc/newsletter-agent/db-tls
   sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj /CN=db -addext subjectAltName=DNS:db \
     -keyout /etc/newsletter-agent/db-tls/server.key -out /etc/newsletter-agent/db-tls/server.crt
   sudo chmod 600 /etc/newsletter-agent/db-tls/server.key
   sudo chmod 644 /etc/newsletter-agent/db-tls/server.crt
   ```

   To copy `AGENT_DB_PASSWORD` into `DATABASE_URL`, open both files in one `sudoedit /etc/newsletter-agent/db.env /etc/newsletter-agent/agent.env`.

   The certificate lets the agent encrypt its database connection and check that it reached the `db` container: the agent always connects with `sslmode=verify-full`, whatever `DATABASE_URL` says, and refuses a connection without TLS. The database accepts only TLS 1.3, and over the network only the agent's account can sign in; the admin account works only inside the `db` container, as in the maintenance commands below. The key stays readable by root only: the `db` container copies it, as root, to a private file of its own each time it starts. The database's accounts are created on Postgres's first start, from `db/init-agent.sql`; "Maintenance" says how to change a password later.

3. **Build the image, start Postgres, and check everything:**

   ```bash
   cd /opt/newsletter-agent
   sudo env REVISION="$(cat REVISION)" docker compose build agent
   sudo docker compose up -d db
   sudo docker compose run --rm -T agent check
   ```

   Every line should start with `ok`, and the first one names the revision you copied. The Claude check makes one small call per model, and the web search check one search session; each line lists the tools the session offered. `--skip websearch` leaves the search out. The `imap` check signs in to Gmail over IMAP with the same app password, reads your Sent folder read-only, and counts your ratings from the last 30 days. Build `agent` by name: it sits under a Compose profile, so a plain `docker compose build` skips it and the old image keeps running.

4. **Install the systemd units:**

   ```bash
   for unit in newsletter-agent.service newsletter-agent-failure.service newsletter-agent.timer; do
     sed "s|@PROJECT_DIR@|/opt/newsletter-agent|" "deploy/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
   done
   sudo systemctl daemon-reload
   ```

5. **Run once by hand and watch the log:**

   ```bash
   sudo systemctl start newsletter-agent.service
   sudo journalctl -u newsletter-agent -f
   ```

   The first run stores most existing entries as a baseline and scores at most `MAX_ITEMS_PER_RUN` items.

6. **Check the failure email** by sending one test notice:

   ```bash
   sudo systemctl start newsletter-agent-failure.service
   ```

7. **Turn on the daily timer:**

   ```bash
   sudo systemctl enable --now newsletter-agent.timer
   systemctl list-timers newsletter-agent.timer
   ```

   The timer fires at 07:00 Pi time, give or take 15 minutes. If the Pi was off at that time, the run happens at the next boot.

## Running it

Run these on the Pi from `/opt/newsletter-agent`.

| Task | Command |
|---|---|
| Check the configuration and every connection | `sudo docker compose run --rm -T agent check` |
| Run now | `sudo systemctl start newsletter-agent.service` |
| Read today's log | `sudo journalctl -u newsletter-agent --since today` |
| See recent runs | `sudo docker compose exec db psql -U newsletter_agent -c "select id, started_at, status, error from runs order by id desc limit 5"` |
| Resend digests that failed to send | `sudo docker compose run --rm -T agent send-pending` |
| Score older entries of one source | `sudo docker compose run --rm -T agent backfill --newsletter ai --source anthropic --since 2026-10-01` |
| Pause the daily run | `sudo systemctl disable --now newsletter-agent.timer` |

**Exit codes:**

- `run` exits 0 when the run succeeded or partly succeeded.
- It exits 2 when it failed and sent the failure email.
- It exits 1 when it failed and couldn't send one, or when it was still running 15 minutes past `RUN_DEADLINE_MINUTES` and stopped itself. systemd then starts `newsletter-agent-failure.service`, which tries again.

**Rating items.** Under each item in your own digests are two links, "More like this" and "Less like this". Tapping one opens an email to yourself with a subject such as `newsletter-agent feedback more 1234 3f9a0c51d2e7`; send it as it is. The last part names your database, so ratings from a test database or one that was rebuilt are never mixed in. The next run reads it from your Sent folder and stores the rating:
- Scoring then shows Claude your latest 5 likes and 5 dislikes from the last 60 days, as examples that refine the newsletter's `interests.md`. The profile still decides what's in scope.
- Your footer counts your ratings by source, which helps decide which sources to keep. If ratings couldn't be read, the footer says why, and the run still goes on.
- Rating an item again replaces the earlier rating. Deleting the email after the run doesn't undo a rating.
- A digest for anyone else has no links.

**On a Mac,** if clicking a rating link in Apple Mail does nothing, open Mail → Settings → General and set "Default email reader" to Mail. A click then opens a new message in Apple Mail; send it from the Gmail account in `SMTP_USERNAME`, so it lands in the Sent folder the agent reads.

See the stored ratings with:

```bash
sudo docker compose exec db psql -U newsletter_agent -c "select f.sent_at, f.liked, i.title from feedback f join items i on i.id = f.item_id order by f.sent_at desc limit 20"
```

**A stuck run.** If a scheduled run's container ever outlives its unit, the next scheduled run can't start: Docker refuses a second container named `newsletter-agent-run`, so that unit fails at once. Its clean-up step removes the old container, and you get the generic notice that the run stopped before it could report. The run after that marks the old run `abandoned` and works normally. A run you started by hand with `docker compose run` is different: if it hangs, it keeps the run lock, and each scheduled run sends a failure email saying a run "still holds the run lock". Find its container with `sudo docker ps --filter name=newsletter-agent` and remove it with `sudo docker rm -f <name>`.

**What a missing email means.** No email can mean a quiet day, but also a Pi that's off or a broken Gmail password, because email is the only alert channel. Each digest's footer counts the runs since the last digest. If several days pass without an email, check the log.

## Maintenance

- **Renew the Claude token before 2027-10-30.**
  1. Run `claude setup-token`.
  2. Edit `/etc/newsletter-agent/agent.env` with `sudoedit`: replace `CLAUDE_CODE_OAUTH_TOKEN` and set `TOKEN_CREATED` to the new date.

  Each digest shows the days left, and moves a warning to the top in the last 30 days. An expired token makes the run fail and send a failure email.

  GitHub has its own copy, the repository secret `CLAUDE_CODE_OAUTH_TOKEN`, which Claude review and `@claude` use. When Claude review starts failing with a sign-in error, run `/install-github-app` in Claude Code again, or `gh secret set CLAUDE_CODE_OAUTH_TOKEN` with a new token.
- **After changing your Google password,** create a new app password at https://myaccount.google.com/apppasswords and replace `SMTP_APP_PASSWORD`. Google revokes app passwords when the password changes.
- **Edit sources, search terms or interests** in `config/newsletters/` in the repository, then copy the code to the Pi again (step 1). Don't edit them on the Pi: the next deploy would overwrite the change. The folder is mounted into the container, so the next run uses it without a rebuild. Changing a source's type, URL, channel or include rules makes its next collection start again from a baseline, so old entries aren't scored as new.
- **Turn transcripts off** with `TRANSCRIPTS_ENABLED=false` if YouTube blocks the Pi. Each blocked video shows `IpBlocked` in `item_content.error`, and videos are scored on their title and description either way. YouTube blocked transcript requests from the home network on 2026-10-30, after about two dozen test requests that day.
- **Never edit a migration file that a database has already applied.** Add a new file in `src/newsletter_agent/migrations/`, numbered one more than the highest number there, such as `<next number>_add_column.sql`; the next run applies it.
- **Change a database password** when you want to, or if `db.env` was ever exposed. In `/opt/newsletter-agent`, run `sudo docker compose exec db psql -U postgres -c '\password newsletter_agent'` (or `'\password postgres'` for the admin account), and enter a new value from `openssl rand -hex 24` at the prompt. Then put it in `db.env`, and for the agent's account in `DATABASE_URL` in `agent.env`, with `sudoedit`.
- **Replace the database certificate** before it expires, 10 years after you made it, or if its key was ever exposed: make a new pair with the commands of step 2, then run `sudo docker compose up -d --force-recreate db`. The agent reads the new certificate on its next run.
- **Before upgrading `claude-agent-sdk`,** rebuild and run `check` with the real token, then again with an invalid one. The invalid run must report `ClaudeAuthError`. A new SDK version may change how the OAuth token is read.
- **Stop everything and keep the data** with `sudo docker compose down`.

  **Warning:** `sudo docker compose down -v` deletes every collected item and run, and it can't be undone.

There are no backups: the Postgres data lives only in the `db-data` volume on the Pi.

## Development

```bash
uv sync
brew install gitleaks
uv run pre-commit install
uv run pre-commit run --all-files
uv run pre-commit run --all-files --hook-stage pre-push
```

`pre-commit install` adds two git hooks; run it once per clone. The fast checks run on every `git commit`, and the slower ones, types, duplicates, the vulnerability audit and the gitleaks secret scan, run on every `git push`. The last two commands run each stage by hand.

**Making a change.** Changes reach `main` only through a pull request:

1. Open an issue for the change.
2. Work on a branch, and push it.
3. Open a pull request with `Closes #<issue number>` in its description, or link the issue under Development in the pull request's sidebar, then re-run the PR hygiene job.

GitHub Actions then runs 17 checks, from PR hygiene to the secret scan. ARCHITECTURE.md section 12 says what each one checks. One of them, Claude review, has Claude review the pull request. Every problem it finds blocks the merge, however minor: anything wrong, unclear, inconsistent or out of date. It posts an inline comment for each, and the check fails. Its verdict on your latest commit and a summary of what it checked go in one comment on the pull request, which it edits on each later review. It only reviews: it can read the pull request and comment on it, but never change a file, commit or push, and `scripts/check_review_workflow.py` checks the settings that keep it so. Fix the findings and push, which runs it again. If Claude is wrong, add the `review-override` label after your latest push. Adding it runs Claude review and the Workflow guard again, and both pass for that commit only, even if you add other labels later: the next push needs Claude's review again, or the label removed and added again. The Workflow guard fails a pull request that changes `.github/`, `scripts/` or a check's settings file until you add the label. To ask it something on a pull request or an issue, write a comment that mentions `@claude`, such as `@claude check this against CLAUDE.md`. Both use your Claude plan, and only answer you. A rule on `main` blocks direct pushes and any merge while a check is red, Claude review included.

**This repository is public.** Keep personal details, your machines' network and access details, and secrets out of every commit, issue and comment. Git ignores any file with `.local` in its name, so private notes can live in one, such as `CLAUDE.local.md`, which Claude Code also loads beside `CLAUDE.md`.

To run against a local Postgres in Docker, put `db.env`, `agent.env` and a `db-tls` folder made as in step 2 of "Deploy on the Pi" in a folder outside the repository, then:

```bash
export NEWSLETTER_AGENT_ENV_DIR=<that folder>
docker compose -p newsletter-agent-local up -d db
docker compose -p newsletter-agent-local build agent
docker compose -p newsletter-agent-local run --rm -T agent check
docker compose -p newsletter-agent-local run --rm -T agent run
```

For cheap local runs, set `SCORE_MODEL`, `DIGEST_MODEL` and `SEARCH_MODEL` to `claude-haiku-5-5` and `MAX_ITEMS_PER_RUN` to about 12.

## Author

This project is maintained by [Bez Moradi](https://github.com/bezmoradi).

## License

Newsletter agent is licensed under [MIT](LICENSE).
