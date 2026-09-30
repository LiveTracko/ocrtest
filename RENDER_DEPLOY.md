# Deploy to Render (no server knowledge needed)

Your app is now a website. You do everything in the browser.

## What I already did (code side, done)
- `server.py` — web server (upload, progress, download, merge)
- `job_runner.py` — same pipeline, but each upload gets its own private folder
- `templates/index.html` + `static/` — the browser page
- `Dockerfile` + `render.yaml` — Render deploy recipe
- `requirements.txt` — added web packages

## What YOU do (15 minutes, browser only)

### Step 1 — Code is on GitHub (I pushed it)
Repo: https://github.com/LiveTracko/ocrtest

### Step 2 — Create Render account
1. Go to https://render.com → Sign up (use your GitHub account = 1 click).

### Step 3 — Deploy
1. Dashboard → **New +** → **Web Service**.
2. **Connect** your GitHub → select `ocrtest` repo → Connect.
3. Render auto-detects the `Dockerfile`. Settings:
   - Name: `ocrtest`
   - Region: closest to you (e.g. Singapore if users are in India)
   - Plan: **Free** (to start)
4. Before clicking Create: open **Environment** → **Add Environment Variable**:
   - Key: `GEMINI_API_KEY` — Value: paste your key from https://aistudio.google.com
   - (Optional) `GEMINI_MODEL` = `gemini-2.0-flash`
5. Click **Create Web Service**. Wait ~5-10 min (first build installs OCR packages).
6. When status is **Live**, open the URL: `https://ocrtest.onrender.com`
   - You should see "Voter Roll Extractor" + "Server OK".

### Step 4 — Test it
1. Upload a small PDF (1-2 pages first) → watch progress → Download Excel.
2. Then try a full PDF.

### Step 5 — Connect your subdomain (e.g. ocr.yourdomain.com)
1. In Render → your service → **Settings → Custom Domain** → Add `ocr.yourdomain.com`.
   Render shows you a CNAME target (e.g. `ocrtest.onrender.com`).
2. In your main server / domain panel (cPanel → Zone Editor):
   - Add record: Type `CNAME`, Name `ocr`, Value `ocrtest.onrender.com` (your real URL).
   - Wait 5-60 min → `https://ocr.yourdomain.com` works with lock icon.

## Limits you should know (Free plan)
- Sleeps after 15 min idle → first open takes ~1 min to wake. Fix: any UptimeRobot free monitor pinging `/api/health` every 5 min.
- Files are temporary — finished Excels stay until next deploy/restart. **Download results the same day.** (Paid plan + disk = permanent.)
- 2 PDFs process at once, rest wait in queue (protects API quota).
- Big PDFs (200+ pages) can take hours — normal, watch progress in browser.
- If OCR install fails the build or you need faster/always-on → upgrade to **Starter ($7/mo)**. Nothing in code changes.

## If something breaks
- Render Dashboard → **Logs** → copy the red error lines and send them to me.
- `429/quota` = Gemini limit → wait, then use "Retry Failed/Review" mode.
