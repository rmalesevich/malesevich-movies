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

## Deploying to TrueNAS

1. Create a dataset for the app's state, e.g. `/mnt/tank/apps/malesevich-movies`.
2. Copy the repo to the NAS and create `.env` there. Set `DATA_PATH` to the
   dataset path, `HOST_PORT` to whatever port you want, and `PUID`/`PGID` to the
   owner of the dataset.
3. Bring it up:

   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```

Everything stateful — the SQLite database and its WAL — lives in that one
directory, so **a ZFS snapshot of the dataset is a complete backup**. There is
no second database container to manage.

To update: `git pull && docker compose -f docker-compose.prod.yml up -d --build`.
Migrations apply themselves on start.

The app runs with a **single worker on purpose** — the scheduler runs in-process,
and extra workers would run the nightly sync more than once.

---

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
    stats.py         Aggregate queries
    rounds.py        Shared read helpers
  templates/         Jinja2
  static/            app.css, search.js (no CDN, no build step)
alembic/             Migrations
tests/               pytest suite
docker/entrypoint.sh Runs migrations, then the server
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
