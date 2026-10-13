# SchoolOps Excel Sync

Keeps the Fees Calling Desk database up to date with the school's Excel files.
Office staff keep using Excel exactly as before. When a file is saved, this service reads it,
checks it, and updates PostgreSQL.

```
Office saves Excel ─▶ watcher notices (waits until the save is finished)
                   ─▶ reads a COPY of the file (original is never touched)
                   ─▶ validates headings, amounts, phones, duplicates
                   ─▶ plans changes ─▶ dry run: report only
                                    ─▶ apply: one database transaction per file
Every 30 min: full re-check of the folder (catches missed saves)
```

## Safety rules (built in, covered by tests)

| Rule | How |
|---|---|
| Never modify Excel | Files are copied to a temp folder and only the copy is opened, read-only. A test checks the hash and timestamp of every file before and after. |
| No guessed student IDs | A student is matched only by an exact key: the ID column if the school has one, otherwise medium + class + section + Sr. No. + name. A row that only *looks* like an existing student (same Sr. No. or same name) goes to **Review**; a person decides. |
| No duplicates | Duplicate rows in a file are rejected and logged. Re-syncing the same file changes nothing. |
| Never marks anyone "paid" | The system stores what Excel says (Total, Paid, Balance) with a date. A balance of 0 is shown as "No balance in Excel", not "Paid". The call outcome "Says already paid" does not change fees. |
| Payment history kept | Fee values are appended as snapshots; old values are never overwritten. |
| Suspicious money changes held | A balance going **up** or a paid amount going **down** waits in Review (until the school agrees other rules). |
| Call history kept | The sync never writes to `calls` or `messages`. Students are never deleted; a student missing from the latest file is flagged `missing_since`. |
| Layout changes caught | If a sheet's headings change, that sheet is skipped and logged, and its students are not marked missing. |
| Dry run first | `dry_run = true` by default. Dry runs write only a report. |
| Not on real data before confirmation | The service refuses a real folder until `mapping_confirmed = true`. Test mode only runs in folders created by the synthetic generator. |
| No real messages | Messages are previews (English / हिन्दी / मराठी). "Send" stores a record with status `mock_not_sent`. |

## Setup

1. **Install PostgreSQL** (version 14 or newer) and create a database:
   ```sql
   CREATE USER schoolops WITH PASSWORD 'choose-a-strong-password';
   CREATE DATABASE schoolops OWNER schoolops ENCODING 'UTF8';
   ```
2. **Install Python 3.11+**, then in this folder:
   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Try it on fake data first:**
   ```bash
   python -m schoolops_sync make-synthetic ./synthetic_school
   # copy tests/test_config.toml to synthetic.toml, set watch_folder = "./synthetic_school"
   # and database_url to your database, then:
   python -m schoolops_sync --config synthetic.toml init-db
   python -m schoolops_sync --config synthetic.toml check          # dry run, writes nothing
   python -m schoolops_sync --config synthetic.toml sync --apply   # writes to the database
   python -m schoolops_sync --config synthetic.toml serve          # API + watcher on http://127.0.0.1:8000
   ```
   Open http://127.0.0.1:8000/docs to try every endpoint.
4. **Before real school files** (do not skip):
   - Copy `config.example.toml` to `config.toml`. Fill in every `TODO`.
   - Confirm the column headings, where class/section/medium come from, and the payment rules **with the school**.
   - Keep `dry_run = true`. Set `mapping_confirmed = true` and run `check`. Read the report: number of students per class, errors, warnings.
   - Only when the report is correct, set `dry_run = false`.
   - Files must be **.xlsx**. Old **.xls** files are rejected with a message (Save As .xlsx in Excel/WPS).
   - If the folder is on a network drive, set `polling = true`.

## Start on a Mac (synthetic data)

```bash
brew install postgresql@16 python@3.12
brew services start postgresql@16
git clone -b excel-sync-service https://github.com/upadhyeshraddha54-spec/school-automation.git ~/school-automation
cd ~/school-automation/sync_service && ./start_mac.sh
```
Next time, only: `cd ~/school-automation/sync_service && ./start_mac.sh`

The script creates the `schoolops` database, a Python environment, a folder of fake class files
(`synthetic_school/`) and `local.toml` (test mode), then opens http://127.0.0.1:8000.

## Teacher app (Fees Calling Desk)

| Tab | What teachers do there |
|---|---|
| Dashboard | Summary cards, **Call next parent** with the 8 one-tap results, Undo, follow-ups today/overdue, each class against a daily target |
| Students & Fees | **+ Add Excel**, search, filters (medium, class Nursery–10, section, fee status), order by class or highest balance, 25 per page, total/paid/balance, "Changes to check". Click a student for details, fee history, call history and a call form |
| Call Records | Every call with filters, pages, and **Copy for Excel** |
| Messages | English / हिन्दी / मराठी previews with Copy. "Save as test message" only logs; nothing is sent |

**+ Add Excel:** choose one or more .xlsx files → the app checks each file (columns found or missing,
classes, first 8 rows, problems per row, and what would change) → untick files you don't want →
**Import selected files** → confirm → done. Nothing is saved before the confirmation; your Excel
files are never changed (the app works on its own copy and deletes it afterwards). Old .xls files are
refused with a "Save As .xlsx" message. In test mode only synthetic files are accepted.

## Commands

| Command | What it does |
|---|---|
| `init-db` | Create tables (safe to run again) |
| `make-synthetic FOLDER` | Create fake workbooks for testing |
| `check` | Dry run of the whole folder; prints the report |
| `sync --apply [--file NAME] [--force]` | Apply changes; `--force` re-reads unchanged files |
| `serve [--no-watch]` | Start the app/API (and the folder watcher unless `--no-watch`) |

## API (for the UI pages)

| Page | Endpoints |
|---|---|
| Dashboard | `GET /dashboard`, `GET /call-next`, `GET /follow-ups?when=today\|overdue\|upcoming` |
| Students | `GET /students?search=&std=&medium=&section=&fee_status=&page=&page_size=`, `GET /students/{id}`, `GET /filters` |
| Fee Collection | `GET /students?fee_status=due`, `GET /students/{id}` (fee history) |
| Call History | `POST /calls`, `DELETE /calls/{id}` (undo within 10 min), `GET /calls?...` |
| Messages | `GET /messages/preview/{student_id}`, `POST /messages/mock`, `GET /messages` |
| Excel Sync | `GET /sync/status`, `POST /sync/run`, `GET /sync/runs`, `GET /sync/runs/{id}`, `GET /review`, `POST /review/{id}/approve\|reject` |

Review decisions:
- **Fee change**: approve = accept the new Excel values; reject = keep the old values.
- **Possible same student**: approve = same child (name/Sr. No. corrected, call history kept); reject = a different child (added as new).

## Tests

```bash
# needs a PostgreSQL test database
export TEST_DATABASE_URL=postgresql://postgres@localhost:5432/schoolops_test
python -m pytest -q
```
48 tests cover the reader, the sync rules, the watcher, the API and the Add Excel import.
All test data is synthetic.

## Logs
`logs/sync.log` (rotating). Every run, issue and decision is also stored in `sync_runs`, `sync_issues` and `review_items`.
