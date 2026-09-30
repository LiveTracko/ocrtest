# Host the app on GitHub Pages (free, no server)

The app in `docs/` is a pure static website: PDF renders, OCR, Gemini calls
and Excel download all happen **in the visitor's browser**. GitHub Pages
hosts it free.

## One-time setup (5 min, browser only)

1. Open https://github.com/LiveTracko/ocrtest → **Settings** → **Pages**.
2. Under **Build and deployment**:
   - Source: **Deploy from a branch**
   - Branch: **main**, folder: **/docs** → **Save**.
3. Wait ~1-2 min. Your app is live at:
   `https://livetracko.github.io/ocrtest/`

## How each user uses it (no install)

1. Open the link above.
2. Section 1: paste your Gemini API key (free from
   https://aistudio.google.com → Get API key) → **Save key**.
   The key stays in that browser only — it is sent only to Google.
3. Section 2: choose PDF(s), set From/To pages, **Start Processing**.
   Pages go one-by-one from the browser straight to Google.
4. Section 3: preview rows, **Download voters.xlsx / review.xlsx**.

## Notes

- Keep the tab open while processing (closing stops it, finished rows stay).
- Works on desktop Chrome/Edge. Large PDFs take time: ~15-30 sec/page
  (OCR + AI). Start with 1-2 pages.
- To update the site: just `git push` to `main` — Pages redeploys itself.
- The Python pipeline (`main.py`, `app.py`) still works locally for
  desktop use; Pages does not need it.
