"""Employee Attendance & Analytics API."""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


def normalize_datetime(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def truncate_datetime(dt: datetime) -> datetime:
    normalized = normalize_datetime(dt)
    return normalized.replace(microsecond=0)


def datetime_from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    dt = truncate_datetime(dt)
    return int(dt.astimezone(UTC).timestamp() * 1000)


def parse_time_hm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(hour=h, minute=m)


def to_ist(dt: datetime) -> datetime:
    dt = truncate_datetime(dt)
    return dt.astimezone(IST)


def half_up(value: float, digits: int = 2) -> float:
    quant = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def compute_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = to_ist(punch_in)
    shift_start_time = parse_time_hm(shift_start)
    shift_end_time = parse_time_hm(shift_end)
    if shift_end_time <= shift_start_time:
        if local.time() < shift_end_time:
            candidate = local.date() - timedelta(days=1)
        else:
            candidate = local.date()
    else:
        candidate = local.date()
    return candidate.isoformat()


def shift_start_dt_for_record(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    record_date = date.fromisoformat(compute_attendance_date(punch_in, shift_start, shift_end))
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(record_date, time(hour=h, minute=m), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local = to_ist(punch_in)
    start_dt = shift_start_dt_for_record(local, shift_start, shift_end)
    elapsed_seconds = int((local - start_dt).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    start = truncate_datetime(punch_in)
    end = truncate_datetime(punch_out)
    seconds = int((end - start).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_end_dt_for_record(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    record_day = date.fromisoformat(attendance_date)
    end_day = record_day + timedelta(days=1) if parse_time_hm(shift_end) <= parse_time_hm(shift_start) else record_day
    end_time = parse_time_hm(shift_end)
    return datetime.combine(end_day, end_time, tzinfo=IST)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    local = to_ist(punch_out)
    end_dt = shift_end_dt_for_record(attendance_date, shift_start, shift_end)
    elapsed_seconds = int((local - end_dt).total_seconds())
    overtime_minutes = elapsed_seconds // 60
    if overtime_minutes < 30:
        return 0
    return overtime_minutes


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    if "created_at" in out and out["created_at"] is not None:
        out["created_at"] = to_epoch_ms(out["created_at"])
    out.pop("_id", None)
    return out


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    out = {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": doc.get("history", []) or [],
    }
    for key in ("punch_in", "punch_out"):
        out[key] = to_epoch_ms(out[key])
    history = []
    for item in out["history"]:
        entry = dict(item)
        entry["at"] = to_epoch_ms(entry.get("at"))
        changes = {}
        for field, value in entry.get("changes", {}).items():
            changes[field] = {
                "from": to_epoch_ms(value["from"]) if isinstance(value.get("from"), datetime) else value.get("from"),
                "to": to_epoch_ms(value["to"]) if isinstance(value.get("to"), datetime) else value.get("to"),
            }
        entry["changes"] = changes
        history.append(entry)
    out["history"] = history
    return out


def month_bounds(month: str) -> tuple[date, date]:
    if not isinstance(month, str) or not re.fullmatch(MONTH_PATTERN, month):
        raise HTTPException(422, "month must be in YYYY-MM format")
    year, month_no = map(int, month.split("-"))
    try:
        first = date(year, month_no, 1)
        next_month = date(year + (month_no == 12), (month_no % 12) + 1, 1)
    except ValueError as exc:
        raise HTTPException(422, "invalid month") from exc
    last = next_month - timedelta(days=1)
    return first, last


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_punch_times(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in cannot be null")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out cannot be null")
        return self


def month_date_strings(month: str) -> tuple[str, str]:
    start, end = month_bounds(month)
    return start.isoformat(), end.isoformat()


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    month_start, month_end = month_date_strings(month)
    weekday = {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            [2, 3, 4, 5, 6],
        ]
    }
    present = {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "_joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
            "_month_start": {"$dateFromString": {"dateString": month_start}},
            "_month_end": {"$dateFromString": {"dateString": month_end}},
        }},
        {"$set": {
            "_working_start": {
                "$cond": [
                    {"$gt": ["$_joined_date", "$_month_start"]},
                    "$_joined_date",
                    "$_month_start",
                ]
            }
        }},
        {"$set": {
            "working_days": {
                "$cond": [
                    {"$gt": ["$_joined_date", "$_month_end"]},
                    0,
                    {"$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$range": [
                                            0,
                                            {
                                                "$add": [
                                                    {
                                                        "$dateDiff": {
                                                            "startDate": "$_working_start",
                                                            "endDate": "$_month_end",
                                                            "unit": "day",
                                                        }
                                                    },
                                                    1,
                                                ]
                                            },
                                        ]
                                    },
                                    "as": "offset",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$_working_start",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                            "as": "workday",
                            "cond": {
                                "$in": [
                                    {"$dayOfWeek": "$$workday"},
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                        }
                    }},
                ]
            }
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {
                "code": "$emp_code",
                "month_start": month_start,
                "month_end": month_end,
            },
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", "$$month_start"]},
                    {"$lte": ["$date", "$$month_end"]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [present, weekday]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$cond": [
                        {"$gt": ["$late_minutes", 0]},
                        "$late_minutes",
                        0,
                    ]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ],
            "as": "_monthly",
        }},
        {"$set": {"_totals": {"$ifNull": [{"$arrayElemAt": ["$_monthly", 0]}, {}]}}},
        {"$set": {
            "present_days": {"$ifNull": ["$_totals.present_days", 0]},
            "attendance_pct": {"$cond": [
                {"$gt": ["$working_days", 0]},
                {"$divide": [
                    {"$floor": {"$add": [
                        {"$multiply": [
                            {"$divide": [{"$ifNull": ["$_totals.present_days", 0]}, "$working_days"]},
                            10000,
                        ]},
                        0.5,
                    ]}},
                    100,
                ]},
                None,
            ]},
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "working_days": 1,
            "present_days": 1,
            "leave_days": {"$ifNull": ["$_totals.leave_days", 0]},
            "late_count": {"$ifNull": ["$_totals.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$_totals.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$_totals.total_overtime_minutes", 0]},
            "attendance_pct": 1,
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    month_start, month_end = month_date_strings(month)
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    present = {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
    weekday = {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            [2, 3, 4, 5, 6],
        ]
    }
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [present, weekday]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]}},
                    "work_hours_total": {"$sum": {"$cond": [
                        {"$and": [present, {"$isNumber": "$work_hours"}]},
                        "$work_hours",
                        0,
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": [present, {"$isNumber": "$work_hours"}]},
                        1,
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$cond": [
                        {"$gt": ["$late_minutes", 0]},
                        "$late_minutes",
                        0,
                    ]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "_monthly",
        }},
        {"$set": {"_totals": {"$ifNull": [{"$arrayElemAt": ["$_monthly", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$_totals.present_days", 0]}},
            "work_hours_total": {"$sum": {"$ifNull": ["$_totals.work_hours_total", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$_totals.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$_totals.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$_totals.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$_totals.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$_totals.on_duty_count", 0]}},
        }},
        {"$set": {"avg_work_hours": {"$cond": [
            {"$gt": ["$work_hours_count", 0]},
            {"$divide": [
                {"$floor": {"$add": [
                    {"$multiply": [
                        {"$divide": ["$work_hours_total", "$work_hours_count"]},
                        100,
                    ]},
                    0.5,
                ]}},
                100,
            ]},
            None,
        ]}}},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def leaderboard_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    month_start, month_end = month_date_strings(month)
    employee_pipeline: list[dict[str, Any]] = [
        {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}},
    ]
    if department is not None:
        employee_pipeline.append({"$match": {"department": department}})
    employee_pipeline.append({"$project": {"_id": 0, "name": 1, "department": 1}})
    return [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$lookup": {
            "from": "employees",
            "let": {"code": "$emp_code"},
            "pipeline": employee_pipeline,
            "as": "_employee",
        }},
        {"$unwind": "$_employee"},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$_employee.name"},
            "department": {"$first": "$_employee.department"},
            "total_late_minutes": {"$sum": {"$cond": [
                {"$gt": ["$late_minutes", 0]},
                "$late_minutes",
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]


def department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    start_day = date.fromisoformat(from_date)
    end_day = date.fromisoformat(to_date)
    start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$documents": [{"date": start_dt}]},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [start_dt, end_exclusive],
            },
        }},
        {"$set": {
            "date_key": {"$dateToString": {
                "date": "$date",
                "format": "%Y-%m-%d",
                "timezone": "UTC",
            }}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date_key"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "count"},
            ],
            "as": "_headcount",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date_key"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees",
                    "let": {"code": "$emp_code", "day": "$$day"},
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$emp_code", "$$code"]},
                            {"$lte": ["$joined_on", "$$day"]},
                        ]}}},
                        {"$project": {"_id": 0, "emp_code": 1}},
                    ],
                    "as": "_employee",
                }},
                {"$unwind": "$_employee"},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }},
            ],
            "as": "_attendance",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$_attendance.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_attendance.late_count", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": [
                    {"$floor": {"$add": [
                        {"$multiply": [
                            {"$divide": ["$present_count", "$headcount"]},
                            10000,
                        ]},
                        0.5,
                    ]}},
                    10000,
                ]},
                None,
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "_moving_avg": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$set": {
            "moving_avg_7d": {"$cond": [
                {"$ne": ["$_moving_avg", None]},
                {"$divide": [
                    {"$floor": {"$add": [{"$multiply": ["$_moving_avg", 10000]}, 0.5]}},
                    10000,
                ]},
                None,
            ]}
        }},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0,
            "date": "$date_key",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter: dict[str, str] = {}
        if date_from is not None:
            date_filter["$gte"] = date_from.isoformat()
        if date_to is not None:
            date_filter["$lte"] = date_to.isoformat()
        query["date"] = date_filter
    if status is not None:
        query["status"] = status
    return query


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception as exc:  # pragma: no cover - exercised at runtime
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = truncate_datetime(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    doc = serialize_employee(doc)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punched_at = datetime_from_epoch_ms(body.punched_at) or truncate_datetime(datetime.now(UTC))
    attendance_date = compute_attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punched_at = datetime_from_epoch_ms(body.punched_at) or truncate_datetime(datetime.now(UTC))
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", -1)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    punch_in_dt = truncate_datetime(record["punch_in"])
    punch_out_dt = truncate_datetime(punched_at)
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out_dt - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch_out exceeds 24 hours from punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.50
    updated = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {"$set": updated},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "record changed concurrently")
    record.update(updated)
    return serialize_attendance(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_attendance(d) for d in page_docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: date = Path(alias="date"),
    body: RegularizeIn = ...,
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    record_query = {"emp_code": emp_code, "date": attendance_date.isoformat()}
    state_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
        "history",
    )
    logical_defaults = {
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    for attempt in range(3):
        record = db.attendance_logs.find_one(record_query)
        if not record:
            raise HTTPException(404, "attendance record not found")

        final_status = body.status if body.status is not None else record.get("status")
        final_punch_in = (
            normalize_datetime(record.get("punch_in"))
            if body.punch_in is None
            else datetime_from_epoch_ms(body.punch_in)
        )
        final_punch_out = (
            normalize_datetime(record.get("punch_out"))
            if body.punch_out is None
            else datetime_from_epoch_ms(body.punch_out)
        )

        if final_status in {"ABSENT", "LEAVE"}:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "ABSENT or LEAVE records cannot have punch times")
            final_punch_in = None
            final_punch_out = None
        elif final_status not in PRESENCE_STATUSES:
            raise HTTPException(422, "status must be a valid attendance status")
        elif final_punch_in is None:
            raise HTTPException(422, "presence status requires a punch_in value")

        if final_punch_in is not None:
            final_punch_in = truncate_datetime(final_punch_in)
            if compute_attendance_date(final_punch_in, employee["shift_start"], employee["shift_end"]) != attendance_date.isoformat():
                raise HTTPException(422, "punch_in does not match attendance date")
            if final_punch_out is not None:
                final_punch_out = truncate_datetime(final_punch_out)
                if final_punch_out <= final_punch_in:
                    raise HTTPException(422, "punch_out must be after punch_in")
                if final_punch_out - final_punch_in > timedelta(hours=24):
                    raise HTTPException(422, "punch_out exceeds 24 hours from punch_in")

        if final_status in {"ABSENT", "LEAVE"}:
            updated = {
                "status": final_status,
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            }
        else:
            late_minutes = compute_late_minutes(
                final_punch_in,
                employee["shift_start"],
                employee["shift_end"],
            )
            if final_punch_out is None:
                work_hours = None
                overtime_minutes = 0
                half_day = False
            else:
                work_hours = compute_work_hours(final_punch_in, final_punch_out)
                overtime_minutes = compute_overtime(
                    final_punch_out,
                    employee["shift_start"],
                    employee["shift_end"],
                    attendance_date.isoformat(),
                )
                half_day = work_hours < 4.50
            updated = {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in updated.items():
            old_value = record.get(field, logical_defaults.get(field))
            if isinstance(old_value, datetime):
                old_value = normalize_datetime(old_value)
            if old_value != new_value:
                changes[field] = {"from": old_value, "to": new_value}
        if not changes:
            if attempt:
                raise HTTPException(409, "record changed concurrently")
            raise HTTPException(422, "request changes nothing")

        entry = {
            "at": truncate_datetime(datetime.now(UTC)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        observed = []
        for field in state_fields:
            if field in record:
                observed.append({field: {"$eq": record[field], "$exists": True}})
            else:
                observed.append({field: {"$exists": False}})

        result = db.attendance_logs.update_one(
            {"_id": record["_id"], "$and": observed},
            {"$set": updated, "$push": {"history": entry}},
        )
        if result.matched_count:
            updated_record = db.attendance_logs.find_one({"_id": record["_id"]})
            return serialize_attendance(updated_record)
        if attempt == 2:
            raise HTTPException(409, "record changed concurrently")

    raise HTTPException(409, "record changed concurrently")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=MONTH_PATTERN),
):
    month_bounds(month)
    result = next(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    return {**result, "month": month}


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    month_bounds(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_bounds(month)
    pipeline = leaderboard_pipeline(month, department)
    pipeline.append({"$match": {"rank": {"$lte": limit}}})
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")

    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(
        department,
        from_date.isoformat(),
        to_date.isoformat(),
    )
    return {"department": department, "items": list(db.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
        operation = {
            "find": "attendance_logs",
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explain = db.command({
            "explain": operation,
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        month_bounds(month)
        operation = {
            "aggregate": "employees",
            "pipeline": employee_monthly_pipeline(emp_code, month),
            "cursor": {},
        }
        explain = db.command({"explain": operation, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}
    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        month_bounds(month)
        operation = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(month, department),
            "cursor": {},
        }
        explain = db.command({"explain": operation, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}
    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        month_bounds(month)
        pipeline = leaderboard_pipeline(month, department)
        pipeline.append({"$match": {"rank": {"$lte": limit}}})
        operation = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command({"explain": operation, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(404, "department not found")
        operation = {
            "aggregate": 1,
            "pipeline": department_trend_pipeline(
                department,
                from_date.isoformat(),
                to_date.isoformat(),
            ),
            "cursor": {},
        }
        explain = db.command({"explain": operation, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "database", "explain": explain}
