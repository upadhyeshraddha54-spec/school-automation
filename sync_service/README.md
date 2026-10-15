# SchoolOps – Fees Calling Desk

For **one office staff member** who follows up unpaid school fees by phone. The office keeps
editing its class-wise Excel workbooks as before. This app reads them (never writes to them), shows
who to call today, records what each parent said, schedules the next follow-up, and prepares reminder
drafts in English, हिन्दी and मराठी. **No calls or messages are made automatically.**

```
Excel class files (OneDrive folder, synced to the Mac)
   └─▶ watcher / Add Excel ─▶ validate ─▶ plan (dry run) ─▶ apply ─▶ PostgreSQL
                                             │                     ├─ students, families (parents)
                                             └─▶ review queue ◀────┤─ fee history (append-only)
                                                                   ├─ calls, follow-ups, campaigns
   Browser app (4 tabs) ◀─────────── FastAPI ◀─────────────────────┘─ message drafts
```

## Start on a Mac

```bash
# once
brew install postgresql@16 python@3.12
brew services start postgresql@16

# from your copy of this repository
cd school-automation/sync_service
./start_mac.sh          # opens http://127.0.0.1:8000 with SYNTHETIC students
./start_mac.sh test     # runs the test suite on a separate database
```

`start_mac.sh` creates the `schoolops` database, a Python environment (`.venv`), a folder of fake
class files (`synthetic_school/`) and `local.toml` (test mode). In the app: **Students & Fees →
+ Add Excel** and choose the files in `synthetic_school/`.

## What the app does

| Tab | For the staff member |
|---|---|
| **Dashboard** | Summary (pending fees, parents called today vs. daily target, missed promises, follow-ups due, office actions, Excel changes to check). **Fee campaign** (e.g. start of year): scope by medium/class/section, daily target, progress (parents contacted, amount collected and students cleared according to Excel). **Call next parent**: one entry per parent, siblings together, in priority order: *missed promise → due today → not called yet → no reply for a week*. Up-next list, skip, search and class/medium/section filters. **Follow-ups**: Missed / Today / Upcoming / Office. Per-class progress. |
| **Students & Fees** | **+ Add Excel** (several files → preview, column check, problems, planned changes → confirm → import). Searchable, paginated table with filters (medium, class Nursery–10, section, fee status, follow-up status). Student view: fees, fee history, call and follow-up history, siblings. **Parent view**: every child's class, total/paid/balance and next step, combined total (each child counted once), one call for several children, link/separate siblings. **Excel sync & review**: status, dry-run check, current problems per file/row, "Changes to check", recent syncs. |
| **Call Records** | Every call with filters (incl. result), siblings covered by the same call, Copy for Excel. |
| **Messages** | Pick a parent → tick children → language → draft (one combined message for siblings) → edit → approve → copy. Drafts list with status. Sending is off. |

### Call results and what happens next
| Result | Next follow-up (one open follow-up per child, never duplicated) |
|---|---|
| Will pay | Call back in 3 days |
| Will pay on date | Promise on the chosen date (date required). If the date passes and Excel still shows a balance → **Missed promise** at the top of the queue |
| Part payment | Part-payment promise (date optional, default 7 days; amount optional). Part payments seen in Excel are shown as "Part paid ₹…" |
| Call again | Call back on the chosen date (default tomorrow) |
| No answer / Phone off | Try again tomorrow |
| Wrong number | Office action: fix the number in Excel (out of the calling queue) |
| Says already paid | Office action: check the payment (fees are **not** marked paid) |
| Fee issue | Office action: resolve the dispute (out of the calling queue) |

A new call closes the previous follow-up (as *missed* if it was a promise past its date). When Excel shows
a balance of 0, open follow-ups close as *paid*. Undo (10 minutes) removes the whole call for every child
it covered and restores the previous follow-up.

### Parents and siblings
* One parent (family) → many students. Every student has exactly one family.
* Students are joined automatically **only** by a verified ID column (`[columns] family_id`).
* A **shared phone number never merges anyone**. It creates a "Same parent?" question in
  *Excel sync & review*. "No" is remembered and not asked again.
* Staff can also link or separate siblings in the parent view.

### Excel sync safety (all covered by tests)
* Files are copied to a temp folder; only the copy is opened. Old **.xls** files are read through a
  converted copy (needs `xlrd`); unreadable files are logged as errors, never silently skipped.
* Exact identity matching (ID column, or medium+class+section+Sr. No.+name). Near-matches → review.
  Re-processing the same workbook changes nothing (idempotent). Renamed/moved files are recognised by content.
* Balance going **up** or paid amount going **down** → held for review. Fee values are append-only history.
* Students missing from a file are flagged, never deleted. Calls, follow-ups and drafts are never touched by the sync.
* Layout changes (missing headings) skip only that sheet and are reported.
* Dry run is the default; real folders are refused until the mapping is confirmed; in test mode only synthetic files are accepted.

## Configuration

### Environment variables
| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection, overrides `database_url` in the config | from config |
| `SCHOOLOPS_WATCH_FOLDER` | Folder with the class files, overrides `watch_folder` | from config |
| `TEST_DATABASE_URL` | Separate database for `pytest` | `postgresql://postgres@localhost:5433/schoolops_test` |
| `ONEDRIVE_CLIENT_SECRET` | Only for `auth = "client_credentials"` | – |

No secrets are needed for the local Mac setup (Homebrew PostgreSQL uses your Mac user, local socket only).
If you set a database password, put it in `DATABASE_URL` or in a `.env`/`config.toml` that is **not committed**
(both are in `.gitignore`).

### Database
PostgreSQL 14+. Tables are created/upgraded automatically (`init-db`, or on app start). Manual:
```bash
createdb -E UTF8 schoolops
.venv/bin/python -m schoolops_sync --config config.toml init-db
```

### OneDrive through Microsoft Graph (read-only)
The app can read the class files directly from OneDrive/SharePoint. It only ever sends GET requests
(no upload, rename or delete), downloads changed files (by content tag) into `watch_folder`, and runs
the normal safe sync on them. Renamed/moved files keep their history; files removed in OneDrive are
moved to `watch_folder/.removed/` and their students are kept.

One-time setup (needs a Microsoft 365 admin, ~10 minutes):
1. portal.azure.com → **Microsoft Entra ID → App registrations → New registration**. Name "SchoolOps
   Fees (read-only)", account type "this organization only". No redirect URI.
2. **Authentication** → "Allow public client flows" = **Yes** (needed for device sign-in).
3. **API permissions** → Microsoft Graph → *Delegated* → **Files.Read.All** → Grant admin consent.
4. Copy the **Application (client) ID** and **Directory (tenant) ID** into `[onedrive]` in `config.toml`,
   set `folder_path` (or `drive_id` + `folder_item_id` for a folder shared with the office user), `enabled = true`.
5. `python -m schoolops_sync --config config.toml onedrive-login` → open the link, enter the code, sign in as
   the office user. The token is stored in `data/onedrive_token.json` (git-ignored).
6. `python -m schoolops_sync --config config.toml onedrive-sync` (dry run) → check the report → `serve`.
   `serve` then checks OneDrive every `poll_minutes`. Status and "Sync from OneDrive now" are in
   *Students & Fees → Excel sync & review*.

(App-only access without a user: `auth = "client_credentials"`, an application permission
`Files.Read.All`, admin consent, and the secret in the `ONEDRIVE_CLIENT_SECRET` environment variable.)

### WhatsApp and SMS reminder drafts (never sent)
*Messages → Prepare reminders for a whole list*: missed promises, due today, promised in the next 3 days,
not contacted yet, or all unpaid. One draft per parent per channel per day (siblings combined), using
only amounts from Excel. SMS drafts show their length and number of SMS parts (Hindi/Marathi use
70 characters per part). Staff edit, approve one or many, and copy. `POST /drafts/{id}/send` always refuses:
no provider is connected. Real sending later needs a WhatsApp Business (Cloud API) account with approved
templates and/or a DLT-registered SMS sender and templates — put their names in `[messaging]`.
The same follow-up list can be downloaded as a spreadsheet from the Dashboard ("Download today's list").

### Payments
Payments are only learned from Excel: when Paid goes up (or, without other changes, Balance goes down).
Each change is recorded once (`payments`, one row per fee snapshot); identical re-syncs add nothing,
and simultaneous syncs (watcher, OneDrive, Add Excel) are serialised so nothing is counted twice.
A bigger discount is not a payment. Campaign "collected" totals use these payment records.

### Connecting a local OneDrive folder instead (no Graph)
1. Install the **OneDrive** app on the Mac and sync the shared fees folder. It appears under
   `~/Library/CloudStorage/OneDrive-<Organisation>/…`. In Finder, right-click the folder → **Always Keep on This Device**
   (so files are real files, not online-only placeholders).
2. `cp config.example.toml config.toml` and fill in every `TODO`:
   `watch_folder` (that path), `polling = true`, `recursive` if there are sub-folders, `school_name`.
3. **Column mapping** – open one real class file and copy the exact headings into `[columns]`.
   Do not map columns whose meaning is unclear (e.g. "2500", blank columns, "old fees pending").
   Set where class / section / medium come from in `[classification]`.
4. Agree the **payment rules** with the school (`[payment_rules]`).
5. Keep `dry_run = true`, set `mapping_confirmed = true`, run
   `.venv/bin/python -m schoolops_sync --config config.toml check` and read the report.
6. Only when the report is right: `dry_run = false`, then
   `.venv/bin/python -m schoolops_sync --config config.toml serve` (app + watcher + reconciliation every 30 min).

(Use either this local folder **or** the Graph integration above, not both.)

### Commands
| Command | What it does |
|---|---|
| `init-db` | Create/upgrade tables (safe to repeat) |
| `make-synthetic FOLDER` | Create fake class workbooks (with sibling pairs) |
| `check` | Dry run of the whole folder; prints the report; writes nothing |
| `sync --apply [--file NAME] [--force]` | Apply changes from the folder |
| `serve [--no-watch] [--port N]` | Start the app/API (and the watcher or OneDrive poller unless `--no-watch`) |
| `onedrive-login` | Sign in to Microsoft once (device code, read-only) |
| `onedrive-sync [--apply]` | Download changed files from OneDrive and sync them (dry run unless `--apply`) |

## Backup and recovery
```bash
mkdir -p ~/SchoolOpsBackups
pg_dump -Fc schoolops > ~/SchoolOpsBackups/schoolops-$(date +%F).dump      # daily is enough
# restore into an empty database:
createdb -E UTF8 schoolops_restored
pg_restore -d schoolops_restored ~/SchoolOpsBackups/schoolops-2026-10-15.dump
```
Keep backups **outside** the repository (they contain student data). The Excel files remain the
source of truth for fees; calls, follow-ups and drafts exist only in the database, so back it up.

## Tests
```bash
./start_mac.sh test
# or: TEST_DATABASE_URL=postgresql:///schoolops_test .venv/bin/python -m pytest -q
```
Tests use synthetic data only and cover: reading/validation (incl. real .xls), repeated and changed
syncs, duplicates, renamed/moved files, sub-folders, review decisions, siblings and families, separate
and combined balances, call outcomes and follow-up scheduling, missed promises, payments seen in Excel,
undo, campaigns and targets, message drafts and approval, uploads, the watcher, and the API.

## Not built yet (needs your decision or credentials)
* Real SMS/WhatsApp sending: needs provider accounts and approved templates; `[messaging] enabled` stays `false`
  and the send endpoint refuses regardless.
* Live Microsoft sign-in has not been tested against a real tenant (tests use a fake Graph server).
* Real column mapping and payment rules (need the school's confirmation).
