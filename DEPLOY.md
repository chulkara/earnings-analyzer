# Deploying to Render + Turso (actually $0)

Sources for every claim below: [render.com/docs/free](https://render.com/docs/free)
and [render.com/pricing](https://render.com/pricing), read Sept 2026.

The stack:

- **Render** (Hobby workspace, $0/mo + free compute plan for this service) —
  hosts the Flask app. Free web service specs, per Render's docs:
  - 512 MB RAM
  - Spins down after **15 minutes** with no inbound HTTP or WebSocket traffic
  - Spin-up on the next request takes **about 1 minute** (Render shows a
    branded loading page to the visitor during spin-up)
  - **750 free instance hours per workspace per calendar month** —
    only active (non-spun-down) time counts
  - Ephemeral filesystem: local files are lost on every spin-down, restart,
    or redeploy — this is why we use Turso for durability
  - Free instances can be suspended if they generate an "uncommonly high
    volume" of outbound traffic to external APIs. Our 5-ticker whitelist
    + auto-caching keeps this well inside normal use.
- **Turso** (Starter plan, $0/mo, no expiration) — holds the SQLite cache
  in the cloud so permalinks and the filing cache survive when Render puts
  the container to sleep. Free tier: 9 GB storage, 1 billion row reads/mo,
  25 million writes/mo.
- **`earnings.chulkara.com`** — custom domain is free on Render's Hobby
  workspace (2 included).

Total forever cost: **$0**. Trade-off vs paying: the first visitor after
15 minutes of quiet waits ~1 minute for the container to wake up. Every
request after that is instant until the next idle window. The site's
"About this demo" panel discloses this to visitors directly so there's
no surprise.

---

## 1. Push the repo to GitHub

Render deploys from a GitHub repo — that's the only source of truth it
knows about. Two ways to get the code up there:

**If you already have a repo locally:**

    cd ~/Documents/earnings-analyzer
    git add . && git commit -m "initial commit"
    git push origin main

**If you're starting fresh** (no remote configured yet), pick one path:

Path A — GitHub CLI:

    brew install gh
    gh auth login
    cd ~/Documents/earnings-analyzer
    gh repo create earnings-analyzer --public --source=. --push

Path B — plain git, create the repo on github.com first, then:

    cd ~/Documents/earnings-analyzer
    git init
    git add . && git commit -m "initial commit"
    git branch -M main
    git remote add origin https://github.com/YOUR_USERNAME/earnings-analyzer.git
    git push -u origin main

## 2. Create a free Turso database

Install the Turso CLI. If you don't have Homebrew, use the direct
installer (recommended, one line):

    curl -sSfL https://get.tur.so/install.sh | bash

Then **close and reopen your Terminal** so the installer's PATH change
takes effect. Verify it worked:

    turso --version

Now create the database and grab the two things Render needs:

    turso auth signup
    turso db create earnings-cache
    turso db show earnings-cache --url     # copy the libsql:// URL
    turso db tokens create earnings-cache  # copy the auth token

Save both to a scratch note — the token is displayed once and never
shown again. You'll paste them into Render in step 4.

If you'd rather use Homebrew (`brew install tursodatabase/tap/turso`),
that works too, but requires installing Homebrew first from brew.sh.

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

If the ~1-minute cold start bothers you, add a scheduled cron on
[cron-job.org](https://cron-job.org) (free) to hit
`https://earnings.chulkara.com/healthz` every 10 minutes during hours
someone might visit. Keeps the container warm.

**Watch the math.** A container kept awake 24/7 consumes all 720 hours
(30 days × 24 hours) of a month — comfortably under the 750-hour free
allowance, but it leaves you no headroom if you decide to run a second
free service on the same workspace. Ping it only during your waking
hours (say 8am–midnight, 16 hours/day = 480 hours/month) and you keep
270 hours in reserve.

## Alternative: Fly.io (pay pennies, no cold start)

Fly.io retired its free allowance in October 2024, so it's now pay-as-
you-go. With auto-stop enabled (already in `fly.toml`) the numbers are
tiny — [Fly's pricing docs](https://fly.io/docs/about/pricing/) list
a `shared-cpu-1x` 256 MB machine at $1.94/mo running full-time, or
$0.0027/hr while active. At 20 users doing one analysis each,
realistic monthly cost is ~$0.20 (compute) + $0.15 (1 GB volume) =
about $0.35/mo. Wakes in 1–2 seconds instead of a minute.

The Fly.io files (`Dockerfile`, `fly.toml`) are still in the repo. Set
Fly secrets and `fly deploy` — no `cache.py` changes needed, because
Turso is the same env-var-triggered adapter either way.
