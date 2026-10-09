from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main


def ist_ms(ymd_hms):
    ist = timezone(timedelta(hours=5, minutes=30))
    return int(datetime(*ymd_hms, tzinfo=ist).astimezone(timezone.utc).timestamp() * 1000)


@pytest.fixture
def mock_db():
    fake = mongomock.MongoClient()
    main.db = fake.db
    main.db.command = lambda *args, **kwargs: {"ok": 1}
    return main.db


@pytest.fixture
def client(mock_db):
    with TestClient(main.app) as c:
        yield c


def test_create_employee_and_punch_in(client):
    resp = client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert resp.status_code == 201, resp.text
    emp = resp.json()
    assert emp["emp_code"] == "EMP0001"

    punch = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": ist_ms((2026, 1, 1, 9, 40, 0)), "status": "PRESENT"},
    )
    assert punch.status_code == 201, punch.text
    item = punch.json()
    assert item["date"] == "2026-01-01"
    assert item["late_minutes"] == 0


def test_health_and_startup_indexes(client, monkeypatch):
    assert client.get("/health").status_code == 200

    employee_indexes = main.db.employees.index_information()
    attendance_indexes = main.db.attendance_logs.index_information()
    assert any(
        index.get("unique") and index["key"] == [("emp_code", 1)]
        for index in employee_indexes.values()
    )
    assert any(
        index.get("unique") and index["key"] == [("emp_code", 1), ("date", 1)]
        for index in attendance_indexes.values()
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main.db, "command", unavailable)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "MongoDB unavailable"}


def test_punch_out_and_regularize(client):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Ram Singh",
            "email": "ram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    punch_in_ms = ist_ms((2026, 1, 1, 9, 40, 0))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": punch_in_ms})

    punch_out_ms = punch_in_ms + (8 * 3600 * 1000)
    out = client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": punch_out_ms})
    assert out.status_code == 200, out.text
    data = out.json()
    assert data["work_hours"] == 8.0
    assert data["overtime_minutes"] == 0

    patch = client.patch(
        "/attendance/EMP0002/2026-01-01",
        json={"status": "PRESENT", "punch_in": punch_in_ms, "reason": "Fixed attendance", "regularized_by": "Ops"},
    )
    assert patch.status_code == 422, patch.text

    patch = client.patch(
        "/attendance/EMP0002/2026-01-01",
        json={"status": "WFH", "reason": "Wfh correction", "regularized_by": "Ops"},
    )
    assert patch.status_code == 200, patch.text
    payload = patch.json()
    assert payload["status"] == "WFH"
    assert len(payload["history"]) == 1
    assert payload["history"][0]["by"] == "Ops"


def test_duplicate_punch_in_and_invalid_punch_out(client):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    payload = {"emp_code": "EMP0003", "punched_at": ist_ms((2026, 1, 1, 9, 40, 0))}
    assert client.post("/attendance/punch-in", json=payload).status_code == 201
    assert client.post("/attendance/punch-in", json=payload).status_code == 409

    out = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ist_ms((2026, 1, 1, 18, 30, 0))})
    assert out.status_code == 200
    second = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ist_ms((2026, 1, 2, 9, 30, 0))})
    assert second.status_code in {404, 409}


def test_employee_monthly_and_department_summary(client, monkeypatch):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0004",
            "name": "Alpha",
            "email": "alpha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0004", "punched_at": ist_ms((2026, 1, 5, 9, 30, 0)), "status": "PRESENT"},
    )
    out = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0004", "punched_at": ist_ms((2026, 1, 5, 18, 30, 0))},
    )
    assert out.status_code == 200

    aggregate_calls = []

    def aggregate_employee_pipeline(pipeline):
        aggregate_calls.append(pipeline)
        if "emp_code" in pipeline[0]["$match"]:
            return iter([{
                "emp_code": "EMP0004",
                "working_days": 22,
                "present_days": 1,
                "leave_days": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
                "attendance_pct": 4.55,
            }])
        return iter([{
            "department": "Engineering",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": 9.0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }])

    monkeypatch.setattr(main.db.employees, "aggregate", aggregate_employee_pipeline)
    resp = client.get("/analytics/employees/EMP0004/monthly?month=2026-01")
    assert resp.status_code == 200, resp.text
    values = resp.json()
    assert values["working_days"] == 22
    assert values["emp_code"] == "EMP0004"
    assert values["present_days"] == 1

    dept = client.get("/analytics/departments/summary?month=2026-01")
    assert dept.status_code == 200, dept.text
    summary = dept.json()
    assert summary["month"] == "2026-01"
    assert summary["items"] == [{
        "department": "Engineering",
        "headcount": 1,
        "present_days": 1,
        "avg_work_hours": 9.0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }]
    assert len(aggregate_calls) == 2
    assert "$lookup" in aggregate_calls[0][4]
    assert "$lookup" in aggregate_calls[1][1]


def test_department_trend_and_explain(client, monkeypatch):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0005",
            "name": "Beta",
            "email": "beta@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": ist_ms((2026, 1, 5, 9, 30, 0)), "status": "PRESENT"},
    )
    client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": ist_ms((2026, 1, 5, 18, 30, 0))},
    )

    trend_pipeline = []
    monkeypatch.setattr(
        main.db,
        "aggregate",
        lambda pipeline: (
            trend_pipeline.extend(pipeline)
            or iter([
                {
                    "date": "2026-01-05",
                    "is_working_day": True,
                    "headcount": 1,
                    "present_count": 1.0,
                    "late_count": 0,
                    "attendance_rate": 1.0,
                    "moving_avg_7d": 1.0,
                },
                {
                    "date": "2026-01-06",
                    "is_working_day": True,
                    "headcount": 1,
                    "present_count": 0.0,
                    "late_count": 0,
                    "attendance_rate": 0.0,
                    "moving_avg_7d": 0.5,
                },
            ])
        ),
    )

    trend = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-01-02")
    assert trend.status_code == 200, trend.text
    payload = trend.json()
    assert payload["department"] == "Engineering"
    assert len(payload["items"]) == 2
    assert payload["items"][0]["date"] == "2026-01-05"
    assert payload["items"][1]["moving_avg_7d"] == 0.5
    assert "$documents" in trend_pipeline[0]
    assert any("$densify" in stage for stage in trend_pipeline)
    assert any("$setWindowFields" in stage for stage in trend_pipeline)

    explain = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-01-02")
    assert explain.status_code == 200, explain.text
    assert "explain" in explain.json()


def test_attendance_calculation_boundaries():
    ist = timezone(timedelta(hours=5, minutes=30))

    def local_time(hour, minute, second=0, microsecond=0):
        return datetime(2026, 1, 1, hour, minute, second, microsecond, tzinfo=ist)

    assert main.compute_late_minutes(local_time(9, 40), "09:30", "18:30") == 0
    assert main.compute_late_minutes(local_time(9, 40, 1), "09:30", "18:30") == 10
    assert main.compute_late_minutes(local_time(10, 15, 59), "09:30", "18:30") == 45
    assert main.compute_late_minutes(local_time(9, 40, 0, 900000), "09:30", "18:30") == 0

    record_date = "2026-01-01"
    assert main.compute_overtime(local_time(18, 59), "09:30", "18:30", record_date) == 0
    assert main.compute_overtime(local_time(19, 0), "09:30", "18:30", record_date) == 30
    assert main.compute_overtime(local_time(23, 0), "22:00", "06:00", record_date) == 0
    assert main.compute_overtime(
        datetime(2026, 1, 2, 6, 29, tzinfo=ist),
        "22:00",
        "06:00",
        record_date,
    ) == 0
    assert main.compute_overtime(
        datetime(2026, 1, 2, 6, 30, tzinfo=ist),
        "22:00",
        "06:00",
        record_date,
    ) == 30
    assert main.compute_attendance_date(
        datetime(2026, 1, 2, 5, 0, tzinfo=ist),
        "22:00",
        "06:00",
    ) == "2026-01-01"

    start = datetime(2026, 1, 1, 9, 0, tzinfo=ist)
    assert main.compute_work_hours(start, start + timedelta(seconds=16182)) == 4.5
    assert main.compute_work_hours(start, start + timedelta(seconds=16181)) == 4.49


def test_api_validation_pagination_and_legacy_shape(client):
    invalid_employee = client.post(
        "/employees",
        json={
            "emp_code": "EMP0099",
            "name": "Bad Shift",
            "email": "bad@example.com",
            "department": "Ops",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        },
    )
    assert invalid_employee.status_code == 422

    employee = client.post(
        "/employees",
        json={
            "emp_code": "EMP0098",
            "name": "Valid Employee",
            "email": "valid@example.com",
            "department": "Ops",
            "joined_on": "2026-01-01",
        },
    )
    assert employee.status_code == 201

    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        main.db.attendance_logs.insert_one({
            "emp_code": "EMP0098",
            "date": day,
            "status": "PRESENT",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
        })

    page = client.get("/attendance?emp_code=EMP0098&page=2&page_size=1")
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 1
    legacy = page.json()["items"][0]
    assert legacy["history"] == []
    assert legacy["half_day"] is False
    assert "_id" not in legacy
    assert set(legacy) == {
        "emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
        "late_minutes", "overtime_minutes", "half_day", "history",
    }

    assert client.get("/attendance?status=INVALID").status_code == 422
    assert client.get("/attendance?date_from=2026-02-30").status_code == 422
    assert client.get("/analytics/employees/EMP0098/monthly?month=2026-13").status_code == 422
    assert client.get("/analytics/employees/EMP0098/monthly?month=2026-02x").status_code == 422
    assert client.get("/analytics/departments/Ops/trend?from=2026-02-30&to=2026-03-01").status_code == 422
    assert client.get("/admin/explain/unknown").status_code == 422
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0098", "punched_at": 1.5},
    ).status_code == 422
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0098", "status": "ABSENT"},
    ).status_code == 422

    millis = ist_ms((2026, 1, 4, 9, 40, 0)) + 900
    created_truncated = client.post(
        "/employees",
        json={
            "emp_code": "EMP0096",
            "name": "Precision User",
            "email": "precision@example.com",
            "department": "Ops",
            "joined_on": "2026-01-01",
        },
    )
    assert created_truncated.status_code == 201
    punch = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0096", "punched_at": millis},
    )
    assert punch.status_code == 201, punch.text
    assert punch.json()["punch_in"] == millis - 900
    assert punch.json()["late_minutes"] == 0


def test_regularization_preserves_bson_history_and_appends(client):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0097",
            "name": "History User",
            "email": "history@example.com",
            "department": "Ops",
            "joined_on": "2026-01-01",
        },
    )
    punch_in = ist_ms((2026, 1, 1, 9, 30, 0))
    created = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0097", "punched_at": punch_in},
    )
    assert created.status_code == 201
    first = client.patch(
        "/attendance/EMP0097/2026-01-01",
        json={"status": "WFH", "reason": "Remote day", "regularized_by": "OpsA"},
    )
    second = client.patch(
        "/attendance/EMP0097/2026-01-01",
        json={"status": "ON_DUTY", "reason": "Duty day", "regularized_by": "OpsB"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP0097"})
    assert len(stored["history"]) == 2
    assert isinstance(stored["history"][0]["at"], datetime)
    assert stored["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}
    assert [entry["by"] for entry in stored["history"]] == ["OpsA", "OpsB"]
    assert created.json()["history"] == []
    assert stored["punch_out"] is None

    correction = client.patch(
        "/attendance/EMP0097/2026-01-01",
        json={
            "punch_in": ist_ms((2026, 1, 1, 9, 45, 0)),
            "reason": "Corrected clock time",
            "regularized_by": "OpsC",
        },
    )
    assert correction.status_code == 200, correction.text
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP0097"})
    punch_in_change = stored["history"][2]["changes"]["punch_in"]
    assert isinstance(punch_in_change["from"], datetime)
    assert isinstance(punch_in_change["to"], datetime)
