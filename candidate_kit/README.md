# Employee Attendance & Analytics API

FastAPI and MongoDB service for employee attendance, manual corrections, and attendance analytics. Requires Python 3.11+ and MongoDB 6.0+.

## Run

From this directory, create a virtual environment, install dependencies, and configure `MONGO_URI` and `MONGO_DB` in the environment (or a local `.env` file). The service creates its indexes at startup.

```bash
python -m venv .venv
python -m pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Optionally load the supplied sample documents with `python sample_seed.py`. `GET /health` checks MongoDB readiness.

## Tests

Run unit tests from this directory with `python -m pytest -q`. MongoDB integration tests use a unique temporary database and are skipped unless both `MONGO_TEST_URI` and `MONGO_TEST_ALLOW_DROP=1` are set. Only the generated test database is dropped.

No MongoDB integration or explain-plan results are claimed unless those tests are run against a configured MongoDB 6.0+ server.
