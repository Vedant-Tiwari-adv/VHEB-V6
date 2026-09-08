# VHEB — Building Utility Ledger (web)

A rebuild of the old desktop VHEB app (customtkinter + pyautogui) as a small
web app. Same core idea — split a building's electricity (and a shared
"water" pump cost) across tenants each month and message out the bills — but:

- **No database.** Google Sheets *is* the database, read/written live via
  the Sheets API. Nothing is stored anywhere else.
- **Meters and tenants are fully configurable** from the app — add, rename,
  deactivate, or delete any of them, any time.
- **Suggested pricing** comes from a small RandomForest model trained on
  your own sheet's history, with a hard floor and a heuristic fallback —
  see [app/ml_pricing.py](backend/app/ml_pricing.py).
- **WhatsApp**: every bill is sent to *your own* number first (Meta's
  official Cloud API). You check it, then forward it to the tenant
  yourself — see step 4 below for exactly why this is close to free.
- **No PDFs.** Bills are plain text, previewed in the app before anything
  is sent.

```
vheb-web/
  backend/    FastAPI app (serves the API AND the static frontend)
  frontend/   vanilla HTML/CSS/JS, no build step
  render.yaml Render.com deploy blueprint
```

---

## 0. What you'll need

- Python 3.11+
- A Google account (for Sheets + Drive API access)
- A Meta/Facebook developer account (for WhatsApp Cloud API) — free
- ~20 minutes for one-time setup

---

## 1. Run it locally first

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Leave `.env` half-empty for now — fill it in as you complete steps 2-4 below,
then come back and run:

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, paste your `ADMIN_TOKEN` from `.env`, and you're in
(though most pages will show a "SPREADSHEET_ID is not set" message until step 3).

---

## 2. Google Sheets access (service account)

The app talks to Sheets and Drive as a *service account* — a robot Google
account your code logs in as — rather than your own login, so a deployed
web app doesn't need anyone to click through a browser login flow.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) →
   create a new project (e.g. "VHEB").
2. **APIs & Services → Library** → enable **Google Sheets API** and
   **Google Drive API**.
3. **APIs & Services → Credentials → Create Credentials → Service account**.
   Give it any name, skip the optional role/access steps.
4. Open the new service account → **Keys → Add Key → Create new key → JSON**.
   A `.json` file downloads.
5. Open that JSON file, copy its *entire* contents, and paste them as one
   line into `.env` as `GOOGLE_SERVICE_ACCOUNT_JSON=...`. On Render you'll
   paste the same thing into an environment variable's value (step 5).
6. Note the service account's email address — it looks like
   `something@your-project.iam.gserviceaccount.com`. You'll need it next.

## 3. Create and share the Sheet

1. Create a new blank Google Sheet (sheets.google.com). Any name.
2. **Share** it with the service account's email from step 2.6, giving it
   **Editor** access. (It won't show up as a "person" you'd recognise —
   that's expected, it's the robot account.)
3. Copy the Sheet's ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit` → put it
   in `.env` as `SPREADSHEET_ID=...`.
4. Create the four tabs with the right headers:
   ```bash
   python scripts/setup_sheet.py
   ```
5. **(Optional) Meter photos.** Create a Drive folder, share it with the
   same service account email (Editor), copy its ID from the folder's URL,
   and put it in `.env` as `DRIVE_PHOTOS_FOLDER_ID=...`. Skip this if you
   don't need photos yet — the rest of the app works fine without it.

### Bring in your old Electricity.xlsx history (optional but recommended)

The pricing model gets better with more history, and your old file has
years of it. Dry run first (this only prints, writes nothing):

```bash
python scripts/migrate_legacy.py /path/to/Electricity.xlsx --json-out preview.json
```

Read the "Skipped ..." lines it prints — a handful of very old columns in
that file have inconsistent formatting (see the script's own docstring for
why) and are deliberately left out rather than guessed at. When the summary
looks right:

```bash
python scripts/migrate_legacy.py /path/to/Electricity.xlsx --push
```

**One thing worth checking by hand afterwards:** the March 2026 column in
your file has the main bill amount exactly equal to the main meter reading
(both 45749) — almost certainly a copy-paste slip in the original sheet,
not a real bill. Fix that row in the new `MonthlySummary` tab once it's
migrated, or the pricing model will treat that month as a huge, fake price
spike.

---

## 4. WhatsApp Cloud API

Every bill is sent to **your own WhatsApp number**, not the tenant's — you
check each one, then forward it yourself. That choice makes this genuinely
close to free (see the note in [app/whatsapp.py](backend/app/whatsapp.py)):
Meta only requires an approved *template* (and only then charges a small
per-message rate) for messages sent **outside** an open 24-hour chat
window. Since the recipient of every message is you, opening that window
is entirely in your control — see step 4.5.

1. Go to [developers.facebook.com](https://developers.facebook.com/) → **My
   Apps → Create App → Business** type.
2. Add the **WhatsApp** product to the app. Meta gives you a **free test
   phone number** automatically — good enough to run this whole app.
3. On the WhatsApp → API Setup page:
   - Copy the **Phone number ID** → `.env` as `WHATSAPP_PHONE_NUMBER_ID`.
   - Generate a **permanent access token** (System Users → your business
     app → generate token with `whatsapp_business_messaging` permission —
     the temporary 24-hour token on the API Setup page also works for
     testing). → `.env` as `WHATSAPP_TOKEN`.
   - Under "To", add your own number as a test recipient and verify it
     with the code WhatsApp sends you.
4. Put your own number in `.env` as `ADMIN_WHATSAPP_NUMBER` (international
   format, digits only, e.g. `919373834997`).
5. **Before running the monthly send**, send any message (e.g. "hi") from
   your phone to the WhatsApp test number Meta gave you. That opens the
   free 24-hour window; the app's "Send" step then delivers plain text,
   no template, no charge. If you forget this step, the send will just
   fail with a clear message telling you to do it — nothing is lost.

### Optional: the template fallback

If you'd rather not remember to message first every month, submit a
template once (**WhatsApp Manager → Message Templates → Create**):

- Name: `monthly_bill` (matches `.env`'s `WHATSAPP_TEMPLATE_NAME`)
- Category: **Utility**
- Body:
  ```
  Bill for {{1}}: {{2}} units used, electricity {{3}}, water {{4}}, total {{5}}.
  ```
- Submit for review (usually approved within minutes to a day).

Once approved, the app automatically falls back to it whenever the free
window isn't open — billed at Meta's small utility rate (typically a few
cents/rupees per message; for 6 messages once a month, negligible).

---

## 5. Deploy (Render.com, free tier)

Render's free web-service tier is a good fit: it sleeps after 15 minutes
of inactivity (fine — this only gets used once a month) and needs no
persistent disk (nothing is stored outside the Sheet anyway).

1. Push this project to a GitHub repo.
2. [dashboard.render.com](https://dashboard.render.com) → **New → Blueprint**
   → connect the repo. Render reads `render.yaml` and creates the service.
3. In the service's **Environment** tab, fill in every variable marked
   `sync: false` in `render.yaml` — same values as your local `.env`.
4. Deploy. First load after idling takes ~30-50s (cold start) — normal.

**Alternatives**, if you'd rather not use Render: Fly.io's free shared VMs
don't sleep at all (best if the idle-then-cold-start is annoying); Railway
and PythonAnywhere also work fine for a small FastAPI app like this one —
the only Render-specific file is `render.yaml`, everything else is a
plain `pip install -r requirements.txt && uvicorn app.main:app` app.

---

## 6. Using it month to month

1. **Enter Readings** — fill in each physical meter + the motor's current
   reading, the main meter's reading, and this month's actual bill amount.
   The "no physical meter" tenant's units are calculated automatically.
2. **Preview** — check the breakdown; edit any tenant's total by hand if a
   reading looks off (it's flagged for you, but you make the final call).
   Confirm to save it into the Sheet.
3. **Send** — sends every tenant's bill to your own WhatsApp. Check each
   one, forward it on (native Forward, or the per-tenant link the app
   gives you once it's sent).

---

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest
```

28 tests: the calculation engine (including a hand-checked example),
the pricing model + backtest, WhatsApp send logic (mocked, no real
network calls), and a full API integration test against a fake in-memory
Sheet.

To see how the pricing model would have performed on your *own* sheet's
history:
```bash
python scripts/backtest_report.py --margin 7
```

---

## Security notes

- Everything under `/api/*` (except `/api/health`) requires
  `Authorization: Bearer <ADMIN_TOKEN>`. This is a single shared secret —
  fine for one admin, not real per-user auth. See
  [app/security.py](backend/app/security.py) if you ever need more than one.
- Never commit a filled-in `.env`. `.gitignore` already excludes it.
- The service account's Sheets access is scoped only to sheets you
  explicitly share with it — it can't see your other Drive files.

## Known limitations, stated plainly

- **The pricing floor protects against the last *known* cost, not a
  sudden future spike.** Backtested against six years of this building's
  own data, the model covered the actual bill in the overwhelming
  majority of months; a handful fell a few percent short when the
  utility's rate jumped unexpectedly between bills — no formula can see
  that coming before the bill arrives. A slightly larger margin (10%
  instead of 7%) tightens this further; check `scripts/backtest_report.py`
  after a few months of your own new data.
- **Only one "derived" (no-physical-meter) tenant is supported at a time**
  — the maths only has one degree of freedom to solve for. Give every
  other flat its own meter.
- A natural next step, not built here: track the running gap between
  suggested revenue and actual bills over time, and nudge future months'
  margin up slightly to "catch up" after a shortfall month.
