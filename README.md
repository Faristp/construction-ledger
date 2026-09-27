# Construction Ledger

A small web app for tracking construction-project payments by shareholder,
backed by Postgres.

## Deploy to Render (recommended)

1. Push this folder to a GitHub repo (Render deploys from GitHub).
2. In the Render dashboard: **New +** -> **Blueprint**, pick the repo. Render
   reads `render.yaml` and creates both the free Postgres database and the
   web service, and wires `DATABASE_URL` between them automatically.
3. Render will ask you to fill in one environment variable it can't guess:
   `ADMIN_PASSWORD` - the password you'll use to log in and make edits.
   Pick something you can remember; you can change it later in the web
   service's **Environment** tab (it takes effect on the next restart).
4. Click **Apply**. First deploy takes a few minutes. When it's live, open
   the URL Render gives you (e.g. `https://construction-ledger.onrender.com`).

No `render.yaml`/Blueprint route? Create the two pieces by hand instead:
- **New +** -> **PostgreSQL** (free tier) -> copy its "Internal Database URL".
- **New +** -> **Web Service**, connect the repo, build command
  `pip install -r requirements.txt`, start command
  `uvicorn app:app --host 0.0.0.0 --port $PORT`, and add two environment
  variables: `DATABASE_URL` (the URL you copied) and `ADMIN_PASSWORD`.

Render's free web service sleeps after 15 minutes of no traffic and takes
a few seconds to wake on the next request - normal on the free tier, not a
bug. The free Postgres database does **not** sleep, but Render deletes free
databases after 90 days unless upgraded - keep that in mind for long-term use,
and use **Export CSV** occasionally as a backup either way.

## Run locally

You need a Postgres database to point at - either install Postgres locally,
or use a free one from Render/Neon/Supabase and connect to that even while
developing on your laptop.

    pip install -r requirements.txt
    export DATABASE_URL="postgresql://user:password@host:5432/dbname"
    export ADMIN_PASSWORD="choose-a-password"
    export COOKIE_SECURE=false   # only needed when testing over plain http, e.g. 127.0.0.1
    uvicorn app:app --reload

Open http://127.0.0.1:8000. Tables are created automatically on first run.

## Using it

- Anyone who opens the app can view shareholders, totals, and payments, and
  use **Export CSV**.
- Click **Admin login** and enter `ADMIN_PASSWORD` to add, edit, delete, or
  import payments. The login is a browser cookie, good for 30 days.
- **Import CSV**: export each Google Sheet tab as CSV (File -> Download ->
  Comma Separated Values). Columns needed: `date`, `purpose`, `amount`, and
  optionally a shareholder column (the sample sheet's `Column1` also works).
  `sample_latheef.csv` is included as a ready example.

## Backing up

`Export CSV` any time for a plain-text copy. For a full database backup, use
Render's Postgres dashboard (Backups tab) or `pg_dump "$DATABASE_URL" > backup.sql`.
