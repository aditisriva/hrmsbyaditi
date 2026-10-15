# Employee Attendance & Analytics API

HROne Software Engineer Trainee take-home assignment — FastAPI + MongoDB Atlas.

## What is built

A REST API for employee attendance tracking with punch-in/out, manual corrections, and analytics.

**12 endpoints across 4 groups:**

| Group | Endpoints |
|---|---|
| System | `GET /health` |
| Employees | `POST /employees`, `GET /employees` |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}` |
| Analytics | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend` |
| Admin | `GET /admin/explain/{endpoint}` |

Full contract: `openapi.yaml`. Business rules R1–R10 are in `openapi.yaml` description. Data model is in `DATA_MODEL.md`.

## Tech stack

- **Python 3.12** — FastAPI 0.143, Pydantic v2, uvicorn
- **MongoDB Atlas** (M0 free tier, MongoDB 7) — pymongo 4.18
- **Timezone** — all timestamps stored UTC, dates/shifts in IST (UTC+05:30)

## Local setup

```bash
# 1. Clone and enter the project
cd candidate_kit

# 2. Create virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Mac/Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and set MONGO_URI and MONGO_DB

# 5. Load sample data
python sample_seed.py

# 6. Start the server
uvicorn app.main:app --port 8000 --reload
```

MongoDB **6.0 or newer** is required. MongoDB Atlas free tier (M0) works. For local use: `docker run -p 27017:27017 mongo:7`

## Environment variables

| Variable | Description | Example |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb+srv://user:pass@cluster.mongodb.net/` |
| `MONGO_DB` | Database name | `attendance_db` |

## Run tests

```bash
# Must use the venv Python, not system Python
.venv\Scripts\python.exe -m unittest discover -s tests -v   # Windows
python -m unittest discover -s tests -v                     # Mac/Linux (with venv active)
```

**143 tests, 0 failures.**

## Interactive API docs

Once the server is running, open: `http://localhost:8000/docs`

## MongoDB indexes created at startup

Seven indexes are created automatically when the app starts (idempotent — safe to restart):

| Collection | Index | Purpose |
|---|---|---|
| `employees` | `emp_code` (unique) | Primary employee lookup, duplicate prevention |
| `employees` | `(department, emp_code)` | Department filter on `GET /employees` |
| `attendance_logs` | `(emp_code, date)` (unique) | One record per employee per day, concurrency safety |
| `attendance_logs` | `(date DESC, emp_code ASC)` | `GET /attendance` default sort + date range filter |
| `attendance_logs` | `(emp_code, date DESC)` | Per-employee queries, monthly analytics |
| `attendance_logs` | `(status, date DESC, emp_code ASC)` | Status filter on `GET /attendance` |
| `attendance_logs` | `(emp_code, punch_in ASC)` | Punch-out open-record search |

## Project structure

```
candidate_kit/
├── app/
│   └── main.py          # All application code (single file per spec)
├── tests/
│   └── test_main.py     # 143 unit tests (in-memory fakes, no DB required)
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── sample_seed.py       # Loads sample_data into MongoDB
├── requirements.txt
├── openapi.yaml         # API contract and business rules R1-R10
├── DATA_MODEL.md        # MongoDB schema and sample documents
├── REVIEW.md            # Code review: defects found and fixed
├── DECISIONS.md         # Design decisions (indexes, concurrency, analytics)
└── .env.example         # Environment variable template
```

## Key design decisions

- **Concurrency**: punch-in uses `insert_one` + unique index (no check-then-insert). Punch-out uses atomic `find_one_and_update`. Regularize uses an optimistic-lock history-length guard.
- **Timestamps**: stored as UTC `datetime` in MongoDB, serialised as epoch milliseconds in the API.
- **Analytics**: use MongoDB aggregation pipelines — `$group`, `$lookup`, `$sort`. Derived fields (`work_hours`, `late_minutes`, etc.) are read from stored values, never recomputed.
- **Explain**: uses `db.command("aggregate", ..., explain=True)` — correct for PyMongo 4.x. Output is BSON-safe via `bson.json_util`.

See `DECISIONS.md` for answers to the five specific design questions.
