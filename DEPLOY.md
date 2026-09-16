# Deploying to Render + Turso (actually $0)

The stack:

- **Render** (Hobby plan, $0) — hosts the Flask app. Free web service:
  512 MB RAM, sleeps after 15 min of inactivity, wakes in ~30 seconds on
  next request. 750 hours/month of active time — far more than 20 users
  will use.
- **Turso** ($0 forever) — holds the SQLite cache in the cloud so
  permalinks and the filing cache survive when Render puts the container
  to sleep. Free tier: 9 GB storage, 1B row reads/mo, 25M writes/mo.
- **`earnings.chulkara.com`** — custom domain, free on Render (Hobby
  includes 2 domains).

Total forever cost: **$0**. Trade-off vs Fly.io: cold-start latency is
~30 s the first time someone hits the site after 15 minutes of quiet.
Every request after that is instant until the next idle window.

---

## 1. Push the repo to GitHub

    cd ~/Documents/earnings-analyzer
    git init                # if not already
    git add . && git commit -m "initial commit"
    gh repo create earnings-analyzer --public --source=. --push
    # or: create the repo on github.com and `git push` manually

Render deploys from a GitHub repo; that's the only source of truth it
knows about.

## 2. Create a free Turso database

    brew install tursodatabase/tap/turso   # Mac
    turso auth signup
    turso db create earnings-cache
    turso db show earnings-cache --url     # copy the libsql:// URL
    turso db tokens create earnings-cache  # copy the auth token

Save both — you'll paste them into Render in step 4.

## 3. Create the Render service

Sign in at [dashboard.render.com](https://dashboard.render.com), click
**New → Blueprint**, and point it at your GitHub repo. Render reads
`render.yaml` and provisions everything automatically.

Confirm the plan is **Free** (Render will default there because
`render.yaml` says `plan: free`).

## 4. Set the four secrets

Render will prompt you for each `sync: false` env var listed in
`render.yaml`. Paste in:

- `GROQ_API_KEY` — from console.groq.com
- `ALPHAVANTAGE_API_KEY` — optional; unlock earnings call transcripts
- `TURSO_DATABASE_URL` — from step 2, the `libsql://...` URL
- `TURSO_AUTH_TOKEN` — from step 2

Click **Deploy**. First deploy takes ~2–3 minutes.

## 5. Custom domain — earnings.chulkara.com

In Render dashboard → the service → **Settings → Custom Domains**
→ Add `earnings.chulkara.com`. Render will print DNS instructions.

On chulkara.com's registrar add a CNAME:

    earnings   CNAME   earnings-analyzer.onrender.com

Wait 2–10 minutes; Render provisions the Let's Encrypt cert
automatically once DNS propagates.

## Verifying

- `curl https://earnings.chulkara.com/healthz` returns
  `{"ok": true, "cache_db": "/tmp/cache.db"}`.
- Run an analysis on AAPL, copy the permalink, wait 20 minutes for
  Render to sleep the container, then open the permalink — it should
  still work. That's Turso doing its job; the local `/tmp` cache is
  gone but the run was synced to Turso and gets read back on demand.

## What the free tier limits look like in practice

**Render:**

- 750 hours of active time per month. With auto-sleep, 20 users each
  running one analysis is ~1.5 hours of active time — 0.2% of the
  budget.
- 100 GB egress bandwidth per month. Each dashboard load is ~65 KB
  plus a bit of JSON; you'd need ~1.5M page views to blow through it.
- No persistent disk. That's why we use Turso for durability.

**Turso:**

- 9 GB storage. Each stored filing is ~30-100 KB of text plus a JSON
  analysis of ~5-10 KB. You could cache ~50,000 filings before
  worrying.
- 1B row reads/mo. A dashboard load reads a handful of rows; you'd
  need ~200M page loads to hit it.
- 25M writes/mo. Each fresh analysis writes ~5 rows.

## Rolling out prompt changes

Bump `FULL_ANALYSIS_PROMPT_VERSION` or `QUICK_SENTIMENT_PROMPT_VERSION`
in `analyzer.py` and push. Render redeploys, old cached analyses stay
in Turso (harmless), new runs re-analyze under the new version.

## Logs & shell

    # In Render's dashboard → Logs (live)
    # For DB inspection:
    turso db shell earnings-cache
    > .tables
    > SELECT ticker, form, date FROM runs ORDER BY created_at DESC LIMIT 10;

## Warm-up trick (optional)

If the 30-second cold start bothers you, add a scheduled cron on
[cron-job.org](https://cron-job.org) (free) to hit
`https://earnings.chulkara.com/healthz` every 10 minutes. Uses ~4,300
requests/month of the free-tier budget, keeps the container warm all
the time. Note: this eats your 750-hour Render allowance if left on
24/7, so schedule it only during hours when someone might visit.

## What if you want to go back to Fly.io

The Fly.io files (`Dockerfile`, `fly.toml`) are still in the repo. Set
Fly secrets and `fly deploy` and it'll work — no cache.py changes
needed, because Turso is the same env-var-triggered adapter either
way. Cost on Fly with auto-stop: ~$0.20/month at 20 users.
