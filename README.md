# Malesevich Movies

A self-hosted management and statistics site for the Malesevich movie project:
four people each pick a film, everyone watches all four, and then we come
together to argue about them. Round 1 started **February 19, 2022**.

This app replaces the spreadsheet — it manages the rounds, pulls film metadata
from TheMovieDB, checks Trakt every night to see who has actually watched what,
and computes statistics across the whole history.

---

## Quick start (development, on the MacBook)

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as SECRET_KEY
# paste your TMDB key as TMDB_API_KEY, and set APP_PASSWORD

docker compose up --build
```

The site is then at <http://localhost:8000>. Code is bind-mounted, so uvicorn
reloads on save. Migrations run automatically on every container start.

Running it without Docker works too:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

### Tests

```bash
.venv/bin/python -m pytest tests -q
```

The suite covers the views, the admin flows, the statistics queries, the Trakt
reconciliation and the CSV importer. It uses a throwaway SQLite file and fake
TMDB/Trakt clients, so it never touches the network or your real database.

---

## First run

1. **Add the participants** — *Participants* in the nav. Each person gets a name,
   an optional Trakt username, and the round they joined at. Ryan, Dad and Mom
   join at round 1; the fourth member joins at round 3. Leave *left round* blank
   for anyone still in the lineup.
2. **Import the history** — see below.
3. **Or start fresh** — *Manage Rounds* → create round 1, then pick the films.

---

## Importing the historical rounds

Export the spreadsheet to CSV with one row per pick:

```csv
round,started_on,discussed_on,participant,tmdb_id,title,year,notes
1,2022-02-19,2022-03-19,Ryan,,Heat,1995,
1,2022-02-19,2022-03-19,Dad,275,Fargo,1996,
```

| Column | Required | Notes |
| --- | --- | --- |
| `round` | yes | Round number. |
| `participant` | yes | Created automatically, joining at the first round they appear in. |
| `started_on` | yes, once per round | `2022-02-19`, `02/19/2022`, `19.02.2022` all parse. |
| `discussed_on` | no | Defaults to the day before the next round starts. |
| `tmdb_id` | no | If blank, the title is resolved by searching TMDB. |
| `title`, `year` | if no `tmdb_id` | `year` disambiguates remakes. |
| `notes` | no | Free text on the pick. |

Then:

```bash
# See what would happen without writing anything:
docker compose run --rm web python -m app.cli import-history data/seed/rounds.csv --dry-run

# Do it, and pull each round's Trakt history at the same time:
docker compose run --rm web python -m app.cli import-history data/seed/rounds.csv --sync-trakt
```

The importer pulls full TMDB metadata (runtime, overview, poster, genres, cast
and crew) for every film, skips rounds that already exist, and prints a report
of anything it could not resolve. **Check the `unresolved` section** — a title
that matched a different year is flagged there rather than silently accepted.

### How participant names are matched

Names are matched **ignoring capitalisation and spacing**, so `Ryan`, `ryan`,
`  RYAN  ` and `Ryan  Malesevich` vs `Ryan Malesevich` all resolve to one
person — whether they are already in the database or appear for the first time
mid-file. Genuinely different names (`Ryan` vs `Ryanne`) stay separate.

A new participant is stored with the first spelling the CSV used, minus any
stray spacing; capitalisation is never invented or corrected, so fix it in the
UI if the spreadsheet had it lowercase. Someone already added through the admin
page keeps their existing name and settings — the CSV never overwrites them.

The same rule applies to the *Add participant* form, so the UI cannot mint a
case-variant duplicate either.

### Repairing duplicates from an earlier import

Imports run before this matching existed may have split one person in two. To
find them:

```bash
docker compose exec web python -m app.cli participants
```

Names differing only by case or spacing are flagged at the bottom. Fold one into
the other — picks, watches, ratings and round memberships all move across, and
the widest participation window is kept:

```bash
docker compose exec web python -m app.cli merge-participants ryan Ryan --dry-run
docker compose exec web python -m app.cli merge-participants ryan Ryan
```

The first argument is the duplicate to remove, the second is the record to keep.
Exact spellings are matched first, so the two are addressable even though they
normalise to the same name. If both records picked in the same round, the
duplicate row is dropped rather than duplicated.

There is a worked example at [data/seed/rounds.example.csv](data/seed/rounds.example.csv).

---

## How the daily Trakt sync works

Once a day (04:30 local by default) the app walks every **open** round and, for
each participant who has a Trakt username:

- fetches their movie **history bounded by the round's date window**, and
- fetches their movie **ratings with no date bound at all**.

Only films picked in that round are recorded, but the two record types are
scoped differently on purpose:

| | Scope | Why |
| --- | --- | --- |
| Watch | Inside the round window | "Did you watch it *for this round*" is a question about the window. |
| Rating | Any time | A rating is a standing opinion, not an event. Someone who saw a film years ago and rated it then will never re-rate it just because it came up in a round. |

That second rule is why a film you had already seen and scored — Dark City in
round 76, say — still shows its rating even though the score predates the round
by years. The `rated_at` date is stored as-is, so a carried-over score is
distinguishable from a fresh one.

The job is idempotent — re-running it adds nothing new. It keeps the *earliest*
play inside the window as the watch date and counts replays in `plays`.

**Participants' Trakt profiles must be public.** Reading a public profile needs
only a Client ID, so there is no OAuth flow and no per-user tokens to refresh.
A private profile is reported in the sync log as a 401/403 rather than failing
the whole run. Create the app at <https://trakt.tv/oauth/applications> and put
the Client ID in `TRAKT_CLIENT_ID`.

RSS is deliberately not used: Trakt's feeds only expose recent activity and
carry no TMDB ids or ratings, so they cannot answer "did this person watch
*this specific film* during *this round*".

Recent sync results are shown at the bottom of *Manage Rounds*. You can also
trigger one by hand from that page, or from the CLI:

```bash
docker compose exec web python -m app.cli sync             # every open round
docker compose exec web python -m app.cli sync --round 76  # one round
```

---

## Running a round

The normal rhythm, matching how the project actually works:

1. A round is open. Everyone's picks are set from *Manage Rounds → Edit picks*,
   using the TheMovieDB autocomplete.
2. Through the month the nightly sync ticks the checkboxes on the home page as
   people watch the films.
3. On discussion night, *Manage Rounds* → set the discussion date → **Close
   round N**. Leave "immediately open round N+1" ticked and the next round is
   created with the current lineup, ready for the new picks.

---

## Views

| Path | What it shows |
| --- | --- |
| `/` | The open round: each film with poster, selector, runtime, director, and a checkbox per participant showing who has watched it (with their rating). |
| `/rounds` | Every round, with dates, film count and status. |
| `/rounds/{n}` | One round in full. |
| `/stats` | Average runtime by selector, films selected per person, most covered directors and actors (each expandable to the films and who picked them), genres, and films by release decade. |
| `/admin/participants` | Add, edit and remove participants and their round windows. |
| `/admin/rounds` | Create, close, edit and delete rounds; manual sync; sync log. |

---

## Publishing to GitHub

The NAS deploys by pulling from GitHub, so the local folder needs to be
connected to a repository first. This is a one-time setup.

### 1. Create the repository on GitHub

Create a **private** repository named `malesevich-movies`. Do **not** let GitHub
add a README, `.gitignore` or licence — the folder already has commits, and an
initialised remote would have to be merged before the first push.

With the [`gh` CLI](https://cli.github.com) you can do it from the terminal
instead, which also wires up the remote:

```bash
cd ~/Developer/malesevich-movies
gh auth login                 # once per machine
gh repo create malesevich-movies --private --source=. --remote=origin
```

If you used `gh repo create`, skip to step 4.

### 2. Confirm what you are about to publish

Secrets must not leave the machine. `.env` and the database are already ignored,
but check before the first push rather than after:

```bash
git status --short            # .env must NOT appear here
git check-ignore -v .env      # should print the matching .gitignore rule
```

If `.env` shows up as untracked in `git status`, stop and fix `.gitignore`
before continuing.

### 3. Point the folder at the remote

Copy the URL GitHub shows you. SSH is recommended — the NAS will use the same
remote later, and SSH keys avoid re-entering a token on every pull:

```bash
cd ~/Developer/malesevich-movies
git remote add origin git@github.com:<your-username>/malesevich-movies.git
git remote -v                 # confirm both fetch and push lines
```

Already have an `origin` pointing somewhere else? Replace it rather than adding
a second one:

```bash
git remote set-url origin git@github.com:<your-username>/malesevich-movies.git
```

### 4. Push

```bash
git push -u origin main
```

`-u` sets the upstream, so later pushes are just `git push`. If your local
branch is not `main`, either push it under that name
(`git push -u origin HEAD:main`) or rename it first with
`git branch -M main`.

### 5. Give the NAS read access

The NAS needs to authenticate to pull a private repo. Generate a key **on the
NAS** over SSH:

```bash
ssh truenas_admin@truenas.local
ssh-keygen -t ed25519 -C "truenas-malesevich" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Add that public key to the repository as a **deploy key** (GitHub → the repo →
Settings → Deploy keys → Add deploy key). Leave "Allow write access" unchecked:
the NAS only ever pulls. Then verify from the NAS:

```bash
ssh -T git@github.com        # expect "successfully authenticated"
```

---

## Deploying to TrueNAS

The deployment splits into two halves that stay separate on purpose:

| Half | Where | What it does |
| --- | --- | --- |
| **Build** | SSH, by hand or via `docker/deploy.sh` | `git pull`, then `docker build` the image locally on the NAS |
| **Run** | TrueNAS Apps → Custom App | Runs the already-built image via `docker-compose.prod.yml` |

**They cannot be combined.** TrueNAS runs `docker compose pull` when it deploys
or updates an app, and a pull of a locally-built image fails with
`pull access denied for malesevich-movies, repository does not exist`. So
`docker-compose.prod.yml` deliberately has **no `build:` section** and is
marked `pull_policy: never`; the image has to exist on the box before the app
starts. Your instinct was right — the build is a separate step.

### 1. Prepare the dataset

Create a dataset to hold the repo and the database, e.g.
`/mnt/SSDPool/Application_Data/Malesevich-Movies`. Everything stateful lives
under its `data/` subdirectory, so **a ZFS snapshot of this dataset is a
complete backup** — there is no second database container to think about.

### 2. Clone the repo onto the NAS

```bash
ssh truenas_admin@truenas.local
cd /mnt/SSDPool/Application_Data
git clone git@github.com:<your-username>/malesevich-movies.git Malesevich-Movies
cd Malesevich-Movies
```

### 3. Create the .env on the NAS

`.env` is not in git, so it has to be written on the NAS directly:

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
vi .env
```

Set at minimum:

```ini
SECRET_KEY=<the generated value>
APP_PASSWORD=<your site password>
TMDB_API_KEY=<your TMDB key>
TRAKT_CLIENT_ID=<your Trakt client id>

HOST_PORT=8080
DATA_PATH=/mnt/SSDPool/Application_Data/Malesevich-Movies/data
PUID=568
PGID=568
```

`DATA_PATH` must be an **absolute path** — it is bind-mounted at `/app/data`.
`568:568` is the `apps` user on TrueNAS; make sure it owns the directory:

```bash
sudo mkdir -p /mnt/SSDPool/Application_Data/Malesevich-Movies/data
sudo chown -R 568:568 /mnt/SSDPool/Application_Data/Malesevich-Movies/data
```

### 4. Build the image

```bash
cd /mnt/SSDPool/Application_Data/Malesevich-Movies
sudo docker build -t malesevich-movies:latest .
sudo docker images malesevich-movies      # confirm it exists before the next step
```

The app will not start if this image is missing, because it is never pulled.

### 5. Install the Custom App

TrueNAS → **Apps** → **Discover Apps** → **Custom App** → **Install via YAML**,
and paste exactly this:

```yaml
include:
  - /mnt/SSDPool/Application_Data/Malesevich-Movies/docker-compose.prod.yml
services: {}
```

Note the `-` — `include` takes a list. Everything else (ports, the bind mount,
the uid/gid) comes from `docker-compose.prod.yml` and the `.env` sitting beside
it; Compose resolves both relative to the *included* file, not to wherever
TrueNAS runs from.

The site is then at `http://<nas-address>:8080`. Migrations run automatically
on every container start, so the first boot creates the schema.

### 6. Seed the history

**Your CSV is not on the NAS.** `data/seed/*.csv` is gitignored (and excluded
from the image), so the file never travels with a `git pull` or a build. The
only path into the container is the data bind mount, so copy it across from the
Mac by hand:

```bash
scp data/seed/rounds.csv \
    truenas_admin@truenas.local:/mnt/SSDPool/Application_Data/Malesevich-Movies/data/seed/
```

Then make sure the container's uid can read it — a file arriving with a
restrictive umask is invisible to uid 568:

```bash
ssh truenas_admin@truenas.local
cd /mnt/SSDPool/Application_Data/Malesevich-Movies/data/seed
sudo chown 568:568 rounds.csv
sudo chmod 644 rounds.csv
```

Now import it (see
[Importing the historical rounds](#importing-the-historical-rounds)). The
container's working directory is `/app` and the dataset is mounted at
`/app/data`, so the path is relative to that — not to the repo on the host:

```bash
sudo docker exec -it malesevich-movies \
    python -m app.cli import-history data/seed/rounds.csv --dry-run

# happy with the report? drop --dry-run:
sudo docker exec -it malesevich-movies \
    python -m app.cli import-history data/seed/rounds.csv --sync-trakt
```

Address the container by **name** (`malesevich-movies`), not by the ID shown in
`docker ps` — the ID changes every time the container is recreated. If you ever
need to look the name up:

```bash
sudo docker ps --filter ancestor=malesevich-movies:latest --format '{{.Names}}'
```

### Updating

```bash
ssh truenas_admin@truenas.local
cd /mnt/SSDPool/Application_Data/Malesevich-Movies
./docker/deploy.sh
```

The script pulls, rebuilds, and recreates the container onto the new image.

**Run it without `sudo`.** Docker on TrueNAS needs elevation, so the script
detects that and elevates the docker commands itself, prompting for your
password once. `git pull` deliberately stays unelevated: running it as root
leaves root-owned files in the repo and then trips git's "dubious ownership"
check on the next ordinary pull.

**Restarting the app is not enough.** A restart reuses the image the container
was created from, so it will keep serving the old build with no error to tell
you. The container must be *recreated*. `deploy.sh` handles that by reusing the
compose project TrueNAS created:

```bash
sudo docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' malesevich-movies
```

If you would rather do it from the UI, use **Edit → Save** on the app (which
re-applies the compose file and recreates the container) rather than
**Restart**.

To confirm which build is live:

```bash
sudo docker inspect -f '{{.Image}}' malesevich-movies
sudo docker images --no-trunc -q malesevich-movies:latest    # should match
```

### Troubleshooting: the app goes straight to "Stopped"

TrueNAS reports a container that started and then exited as **Stopped**, with no
indication of why. The reason is always in the container log:

```bash
ssh truenas_admin@truenas.local
sudo docker ps -a --filter name=malesevich-movies      # look at the STATUS/exit code
sudo docker logs malesevich-movies
```

(Or in the UI: **Apps** → the app → **Logs**.) The stopped container is kept, so
the log survives the crash.

| Log says | Cause | Fix |
| --- | --- | --- |
| `cannot open /usr/local/bin/entrypoint.sh: Permission denied`, container `Restarting` | The image was built from a checkout whose files are mode 600, so unreadable files were baked in. Fixed in the Dockerfile — an image built before that fix still carries the problem. | Pull and rebuild: `git pull && sudo docker build -t malesevich-movies:latest .` |
| `/app/data is not writable` | The dataset is not owned by the uid the container runs as | `sudo chown -R 568:568 <DATA_PATH>` on the host |
| `the database directory ... does not exist` | `DATA_PATH` points somewhere that is not there | Create it, or correct `DATA_PATH` in `.env` |
| `unable to open database file` | Same permission problem, from an older image built before the preflight check | Rebuild: `sudo docker build -t malesevich-movies:latest .` |
| `required variable DATA_PATH is missing` | No `.env` beside `docker-compose.prod.yml`, or `DATA_PATH` unset in it | Create `.env` on the NAS (step 3 above) |
| `pull access denied` | The image was not built on the NAS | `sudo docker build -t malesevich-movies:latest .` |

A container stuck in `Restarting` rather than `Exited` is crash-looping because
of `restart: unless-stopped`; the log repeats the same line each time.

To check the two things that go wrong most often:

```bash
# 1. Does the image exist on the NAS?
sudo docker images malesevich-movies

# 2. Does the data directory exist, and who owns it?
stat -c '%n owned by %u:%g mode %a' /mnt/SSDPool/Application_Data/Malesevich-Movies/data
```

That uid:gid must match `PUID`/`PGID` in `.env` (568:568 by default). If it does
not:

```bash
sudo chown -R 568:568 /mnt/SSDPool/Application_Data/Malesevich-Movies/data
```

Then redeploy — remember that **Restart is not enough** if you rebuilt the
image; use `./docker/deploy.sh` or **Edit → Save**.

### Notes

The app runs with a **single worker on purpose** — the scheduler runs in-process,
and extra workers would run the nightly sync more than once.

## Configuration

Every setting is an environment variable, read from `.env`.
See [.env.example](.env.example) for the annotated list.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | — | Signs the session cookie. Set this. |
| `APP_PASSWORD` | *(blank)* | The single site password. Blank disables the login wall. |
| `TMDB_API_KEY` | — | v3 API key or v4 read token; both work. |
| `TRAKT_CLIENT_ID` | — | Trakt application Client ID. |
| `SYNC_ENABLED` | `true` | Set false to disable the nightly job. |
| `SYNC_HOUR` / `SYNC_MINUTE` | `4` / `30` | When the job runs. |
| `TIMEZONE` | `America/New_York` | Timezone the schedule is interpreted in. |
| `DATABASE_URL` | `sqlite:///data/malesevich.db` | Swap for a Postgres URL if the app ever outgrows SQLite. |

The app degrades honestly when a key is missing: without `TMDB_API_KEY` the film
search is disabled with a visible banner, and without `TRAKT_CLIENT_ID` the
scheduler does not start.

---

## Layout

```
app/
  main.py            FastAPI app, login routes, middleware wiring
  config.py          Settings (pydantic-settings)
  db.py              Engine, session, SQLite pragmas (WAL, foreign keys)
  models.py          The schema
  auth.py            Single-password session auth
  scheduler.py       APScheduler daily job
  cli.py             import-history, sync, refresh-metadata, participants
  routers/
    views.py         /, /rounds, /stats
    admin.py         /admin/*
    api.py           /api/tmdb/search  (autocomplete)
  services/
    tmdb.py          TMDB client + metadata persistence
    trakt.py         Trakt client
    sync.py          Reconciles Trakt data against a round
    importer.py      Historical CSV seeding
    participants.py  Name matching (case/spacing) and duplicate merging
    stats.py         Aggregate queries
    rounds.py        Shared read helpers
  templates/         Jinja2
  static/            app.css, search.js (no CDN, no build step)
alembic/             Migrations
tests/               pytest suite
docker/entrypoint.sh Runs migrations, then the server
docker/deploy.sh     NAS update: pull, rebuild, recreate the container
```

### Data model notes

- **Participation is tracked by round number**, not by date — `joined_round` /
  `left_round` on `Participant`. Rounds 1–2 had three people; the fourth joined
  at round 3. `RoundParticipant` then snapshots who was actually in each round,
  so changing the roster later never rewrites history.
- **Watches and ratings are keyed by (round, participant, movie)**, so the same
  film picked again in a later round gets its own viewing records. A rating
  carried over from before the round is copied into each round the film appears
  in, keeping its original `rated_at`.
- **Only the top 20 billed cast and a fixed list of crew jobs are stored** — the
  uncredited long tail only adds noise to the "most covered actors" statistic.
  See `CAST_LIMIT` and `CREW_JOBS` in `app/services/tmdb.py`.
