import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main


@pytest.fixture
def mongo_api():
    uri = os.getenv("MONGO_TEST_URI")
    if not uri or os.getenv("MONGO_TEST_ALLOW_DROP") != "1":
        pytest.skip(
            "set MONGO_TEST_URI and MONGO_TEST_ALLOW_DROP=1 to run isolated MongoDB integration tests"
        )

    mongo = MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        mongo.admin.command("ping")
    except PyMongoError as exc:
        mongo.close()
        pytest.skip(f"MongoDB integration server unavailable: {exc}")

    database_name = f"attendance_test_{uuid.uuid4().hex}"
    database = mongo[database_name]
    previous_db = main.db
    main.db = database
    try:
        with TestClient(main.app) as api:
            yield api, database
    finally:
        mongo.drop_database(database_name)
        main.db = previous_db
        mongo.close()


def employee(code, department, joined_on):
    return {
        "emp_code": code,
        "name": code,
        "email": f"{code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def log(code, day, status, late=0, overtime=0, hours=8.0, half_day=False):
    return {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }


def epoch_ms(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def test_mongodb_atomic_punchout_and_regularization_history(mongo_api):
    api, database = mongo_api
    created = api.post(
        "/employees",
        json={
            "emp_code": "EMP8001",
            "name": "Concurrency",
            "email": "concurrency@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        },
    )
    assert created.status_code == 201

    created = api.post(
        "/employees",
        json={
            "emp_code": "EMP8002",
            "name": "Punch-in race",
            "email": "punch-in-race@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        },
    )
    assert created.status_code == 201
    punch_in_barrier = Barrier(2)

    def punch_in():
        punch_in_barrier.wait()
        return api.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP8002",
                "punched_at": epoch_ms(2026, 1, 5, 4, 0),
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        punch_in_statuses = list(pool.map(lambda _: punch_in(), range(2)))
    assert sorted(punch_in_statuses) == [201, 409]
    assert database.attendance_logs.find_one({"emp_code": "EMP8002"})["history"] == []

    punch_in = epoch_ms(2026, 1, 5, 4, 0)
    assert api.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP8001", "punched_at": punch_in},
    ).status_code == 201

    barrier = Barrier(2)

    def punch_out():
        barrier.wait()
        return api.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP8001", "punched_at": epoch_ms(2026, 1, 5, 13, 0)},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        punch_out_statuses = list(pool.map(lambda _: punch_out(), range(2)))
    assert sorted(punch_out_statuses) == [200, 409]
    assert database.attendance_logs.find_one({"emp_code": "EMP8001"})["history"] == []

    regularization_barrier = Barrier(2)

    def regularize(status, actor):
        regularization_barrier.wait()
        return api.patch(
            "/attendance/EMP8001/2026-01-05",
            json={
                "status": status,
                "reason": f"Correction by {actor}",
                "regularized_by": actor,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        corrections = list(pool.map(
            lambda values: regularize(*values),
            [("WFH", "hr.one"), ("ON_DUTY", "hr.two")],
        ))
    assert [response.status_code for response in corrections] == [200, 200]

    record = database.attendance_logs.find_one({"emp_code": "EMP8001"})
    assert len(record["history"]) == 2
    assert {entry["by"] for entry in record["history"]} == {"hr.one", "hr.two"}
    assert all(isinstance(entry["at"], datetime) for entry in record["history"])
    assert record["history"][0]["changes"]["status"]["from"] == "PRESENT"
    assert record["history"][1]["changes"]["status"]["from"] in {"WFH", "ON_DUTY"}


def test_mongodb_monthly_and_department_summary_edge_cases(mongo_api):
    api, database = mongo_api
    database.employees.insert_many([
        employee("EMP8101", "Ops", "2026-07-06"),
        employee("EMP8102", "Ops", "2026-07-06"),
        employee("EMP8103", "Ops", "2026-08-01"),
        employee("EMP8104", "Idle", "2026-07-01"),
    ])
    database.attendance_logs.insert_many([
        log("EMP8101", "2026-07-06", "PRESENT", late=20, overtime=30, hours=8.0),
        log("EMP8101", "2026-07-04", "PRESENT", late=10, overtime=40, hours=6.0),
        log("EMP8102", "2026-07-07", "WFH", hours=4.49, half_day=True),
    ])

    monthly = api.get("/analytics/employees/EMP8101/monthly?month=2026-07")
    assert monthly.status_code == 200, monthly.text
    assert monthly.json() == {
        "emp_code": "EMP8101",
        "month": "2026-07",
        "working_days": 20,
        "present_days": 1,
        "leave_days": 0,
        "late_count": 2,
        "total_late_minutes": 30,
        "total_overtime_minutes": 70,
        "attendance_pct": 5.0,
    }

    summary = api.get("/analytics/departments/summary?month=2026-07")
    assert summary.status_code == 200, summary.text
    rows = {item["department"]: item for item in summary.json()["items"]}
    assert rows["Ops"] == {
        "department": "Ops",
        "headcount": 2,
        "present_days": 1.5,
        "avg_work_hours": 6.16,
        "late_count": 2,
        "total_late_minutes": 30,
        "leave_count": 0,
        "on_duty_count": 0,
    }
    assert rows["Idle"] == {
        "department": "Idle",
        "headcount": 1,
        "present_days": 0,
        "avg_work_hours": None,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }


def test_mongodb_leaderboard_competition_ties_and_cutoff(mongo_api):
    api, database = mongo_api
    database.employees.insert_many([
        employee(f"EMP82{number:02d}", "Rank", "2026-01-01")
        for number in range(1, 5)
    ])
    database.attendance_logs.insert_many([
        log("EMP8201", "2026-07-06", "PRESENT", late=100),
        log("EMP8202", "2026-07-06", "PRESENT", late=90),
        log("EMP8203", "2026-07-06", "PRESENT", late=90),
        log("EMP8204", "2026-07-06", "PRESENT", late=50),
    ])

    response = api.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert response.status_code == 200, response.text
    assert [
        (item["rank"], item["emp_code"], item["total_late_minutes"])
        for item in response.json()["items"]
    ] == [
        (1, "EMP8201", 100),
        (2, "EMP8202", 90),
        (2, "EMP8203", 90),
    ]


def test_mongodb_trend_gap_fill_moving_average_and_explain(mongo_api):
    api, database = mongo_api
    database.employees.insert_many([
        employee("EMP8301", "Trend", "2026-07-06"),
        employee("EMP8302", "Trend", "2026-07-08"),
        *[
            employee(f"EMP84{number:02d}", "Round", "2026-07-01")
            for number in range(1, 33)
        ],
    ])
    database.attendance_logs.insert_many([
        log("EMP8301", "2026-07-06", "PRESENT"),
        log("EMP8301", "2026-07-08", "PRESENT"),
        log("EMP8302", "2026-07-13", "WFH", half_day=True),
        log("EMP8401", "2026-07-06", "PRESENT"),
    ])

    response = api.get(
        "/analytics/departments/Trend/trend?from=2026-07-06&to=2026-07-13"
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 8
    assert items[0]["date"] == "2026-07-06"
    assert items[1]["attendance_rate"] == 0.0
    assert items[2]["headcount"] == 2
    assert items[2]["attendance_rate"] == 0.5
    assert items[5]["attendance_rate"] is None
    assert items[5]["moving_avg_7d"] == 0.3
    assert items[7]["attendance_rate"] == 0.25
    assert items[7]["moving_avg_7d"] == 0.15

    explain = api.get(
        "/admin/explain/employee_monthly?emp_code=EMP8301&month=2026-07"
    )
    assert explain.status_code == 200, explain.text
    explanation = explain.json()["explain"]
    cursor_explain = explanation["stages"][0]["$cursor"]
    assert cursor_explain["executionStats"]["nReturned"] == 1

    def has_stage(value, stage):
        if isinstance(value, dict):
            return value.get("stage") == stage or any(
                has_stage(child, stage) for child in value.values()
            )
        if isinstance(value, list):
            return any(has_stage(child, stage) for child in value)
        return False

    assert has_stage(explanation, "IXSCAN")
    assert not has_stage(explanation, "COLLSCAN")

    explain_trend = api.get(
        "/admin/explain/department_trend"
        "?department=Trend&from=2026-07-06&to=2026-07-13"
    )
    assert explain_trend.status_code == 200, explain_trend.text
    trend_explanation = explain_trend.json()["explain"]
    densify_explain = next(
        stage
        for stage in trend_explanation["stages"]
        if "$_internalDensify" in stage
    )
    assert densify_explain["nReturned"] == 8
    assert "executionTimeMillisEstimate" in densify_explain

    rounded = api.get(
        "/analytics/departments/Round/trend?from=2026-07-06&to=2026-07-06"
    )
    assert rounded.status_code == 200, rounded.text
    assert rounded.json()["items"][0]["attendance_rate"] == 0.0313
