# DEPLOY.md — Launching xrpldashboard

Step-by-step launch guide. The code is ready; this doc covers the manual
steps you have to do yourself (creating accounts, configuring DNS, etc.).

**Estimated time:** 60–90 minutes for first deploy. Future deploys are
"git push" only.

**Estimated monthly cost:** ~$13/month (with annual Plausible billing)
- Render Starter: $7/mo (always-on hosting)
- Plausible Analytics: ~$4.17/mo (privacy-respecting; annual plan, ~$50/yr)
- Cloudflare DNS: free
- UptimeRobot: free
- Domain renewal: ~$1.50/mo (already owned)

---

## Phase 1 — Git & GitHub (already done ✅)

Git is initialized; remote is `github.com/Enkryptedx/xrpldashboard`.

The launch prep was committed across three logical commits, in order:

1. **Footer** — disclaimer, source link, and contact in `templates/index.html`.
2. **Server-side scan caching** — 60s TTL cache in `amm_scan_pools.py`,
   wired into `app.py`, with a "served from cache" indicator in the page.
3. **Production deployment config** — gunicorn (`app.py` + `Procfile`),
   `requirements.txt`, `render.yaml`, `README.md`, and this `DEPLOY.md`.

Verify by visiting `github.com/Enkryptedx/xrpldashboard` — the new files
(`requirements.txt`, `render.yaml`, `Procfile`, `README.md`, `DEPLOY.md`)
and the modified ones (`app.py`, `amm_scan_pools.py`, `templates/index.html`)
should all be visible. Then push if you haven't already:

```sh
cd ~/Desktop/xrpl_test
git push origin main
```

---

## Phase 2 — Render hosting (15 min)

1. Go to <https://render.com> and sign up (free, no credit card to start;
   add card when you upgrade to Starter plan).
2. Connect your GitHub account when prompted.
3. Click **New → Blueprint** (not "Web Service" — Blueprint reads your
   `render.yaml` automatically).
4. Pick the `xrpldashboard` repo. Render reads `render.yaml` and
   pre-fills everything: name, build command, start command, Python
   version, env vars.
5. Review the auto-detected config matches what `render.yaml` says.
6. Click **Apply**.
7. First build takes 3–5 minutes. Watch the build log — should end with
   "Your service is live at https://xrpldashboard.onrender.com" (or
   similar generated URL).
8. Visit that URL. You should see your dashboard with all 19 pools.

If anything fails during build:
- Check the build log for the specific error
- Most common: missing dependency, version mismatch
- Push a fix to GitHub, Render auto-redeploys

---

## Phase 3 — Custom domain (15 min)

Connect `xrpldashboard.com` to your Render service. The dashboard lives at
the apex domain — no subdomain. Both the apex and `www` resolve to the
dashboard so visitors who type either reach the same place.

1. In Render: **Settings → Custom Domains → Add Custom Domain**.
   Add both `xrpldashboard.com` AND `www.xrpldashboard.com`.
2. Render shows you DNS records to set:
   - `xrpldashboard.com` (apex): A record pointing to Render's IP
   - `www.xrpldashboard.com`: CNAME to your Render URL
3. Set those at your registrar (or in Cloudflare if you've already moved
   nameservers — see Phase 4).
4. Wait 5–30 minutes for DNS propagation.
5. Render auto-provisions an SSL cert. Visit
   <https://xrpldashboard.com>. Should load the dashboard.

---

## Phase 4 — Cloudflare in front (optional but recommended, 10 min)

Why: free DDoS protection, free SSL, free CDN, free analytics. Sits in
front of Render and protects the origin.

1. Sign up at <https://cloudflare.com> (free plan).
2. Click **Add a site**, enter `xrpldashboard.com`.
3. Cloudflare scans your existing DNS records and shows them. Verify
   the ones from Phase 4 are picked up.
4. Cloudflare gives you two nameservers (e.g., `bob.ns.cloudflare.com`).
   Update your registrar's nameservers to these. (Search your registrar's
   docs for "change nameservers".)
5. Wait up to 24 hours for nameserver change to propagate (usually <1 hour).
6. Once Cloudflare shows your site as "Active": SSL/TLS → set encryption
   mode to **Full (strict)**. This ensures end-to-end TLS.

---

## Phase 5 — Plausible Analytics (10 min)

Privacy-respecting analytics. Aligned with your public-good mission
(no cookies, no tracking, no Google).

1. Sign up at <https://plausible.io> (~$50/yr annual plan, or $9/mo monthly; 30-day free trial).
2. Add `xrpldashboard.com` as a site.
3. Plausible gives you a one-line script tag:
   ```html
   <script defer data-domain="xrpldashboard.com" src="https://plausible.io/js/script.js"></script>
   ```
4. Paste it into `templates/index.html` inside `<head>`, just before
   `</head>`.
5. Commit + push:
   ```sh
   git add templates/index.html
   git commit -m "Add Plausible analytics"
   git push
   ```
6. Render auto-deploys. Wait ~3 minutes.
7. Visit your live site. Reload a few times. Check the Plausible
   dashboard — you should see your visits appear within a minute.

---

## Phase 6 — UptimeRobot monitoring (5 min)

Free, alerts you if the site goes down.

1. Sign up at <https://uptimerobot.com> (free plan, 50 monitors).
2. Add Monitor:
   - Type: **HTTPS**
   - Name: `xrpldashboard`
   - URL: `https://xrpldashboard.com/healthz` (the lightweight
     health endpoint — checks every 5 min without doing an XRPL scan)
   - Monitoring interval: 5 minutes
3. Add notification: email or SMS (free). You'll get pinged if the
   site returns non-200 for two consecutive checks.

---

## Phase 7 — Verify launch (5 min)

Final smoke test:

- [ ] <https://xrpldashboard.com> loads
- [ ] Dashboard shows 19 pools sorted by TVL
- [ ] "Last scanned" timestamp is current
- [ ] Filter input hides/shows rows correctly
- [ ] `/lookup?currency=...&issuer=...` returns the inline detail card
- [ ] `/healthz` returns `{"status":"ok"}`
- [ ] Plausible dashboard shows your visit
- [ ] UptimeRobot shows the monitor as "up"

You're live.

---

## Future deploys

Every push to `main` auto-deploys. Workflow:

```sh
# Make a change
git add -A
git commit -m "What you changed"
git push
```

Render redeploys automatically (~3 min). Watch the deploy log for errors.

If a deploy breaks the live site: Render keeps your previous deploy and
you can rollback in **one click** from the Render dashboard. No data loss.

---

## Things deliberately NOT in this guide

- **Database setup.** Not needed yet. The cached scan is in-memory; on
  service restart, the cache rebuilds in 1–2s. When you add historical
  tracking (snapshot pools every 15 min), you'll add a database — that's
  a separate doc.
- **CDN for static assets.** The page is small; Cloudflare's free CDN
  handles it. Optimize later if you start serving images/charts.
- **Multi-region deployment.** Render Starter is single-region. Fine for
  v1. Optimize later if you have global users complaining of latency.
- **Open-sourcing.** When you're ready, switch the GitHub repo to
  Public, add a LICENSE file (MIT or Apache 2.0 are both reasonable),
  and add a CONTRIBUTING.md. Not urgent for launch.

---

## Cost summary (monthly)

| Item | Cost |
|---|---|
| Render Starter | $7 |
| Plausible (annual plan: ~$50/yr) | ~$4.17 |
| Cloudflare DNS + DDoS + SSL | $0 |
| UptimeRobot | $0 |
| Domain renewal (xrpldashboard.com) | ~$1.50 |
| **Total** | **~$13/month** |

Scales as you grow. Render Standard ($25/mo) when you outgrow Starter.
Plausible Plus ($19/mo) when you exceed 100k pageviews/month.
Both are nice problems to have.
