"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)],
        name="attendance_employee_punch_in",
    )


@app.on_event("startup")
def initialize_database():
    create_indexes()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_calendar_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Date must use YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise ValueError("Date must use YYYY-MM-DD format")
    return parsed


def parse_month(value: str) -> tuple:
    """Parse a YYYY-MM string, return (year, month). Raises HTTPException 422 on bad format."""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", value):
        raise HTTPException(422, "month must be YYYY-MM format")
    year, month = int(value[:4]), int(value[5:])
    return year, month


def month_date_range(year: int, month: int) -> tuple:
    """Return (first_day_str, last_day_str) for the given year/month."""
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return first_day.isoformat(), last_day.isoformat()


def epoch_ms_to_datetime(value: int) -> datetime:
    return (EPOCH + timedelta(milliseconds=value)).replace(microsecond=0)


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    delta = normalize_utc(value) - EPOCH
    return delta.days * 86400000 + delta.seconds * 1000


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = normalize_utc(punch_in).astimezone(IST)
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_boundary(day: date, shift_time: str) -> datetime:
    hour, minute = map(int, shift_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "23:59") -> int:
    instant = normalize_utc(punch_in).astimezone(IST)
    shift_day = attendance_date(punch_in, shift_start, shift_end)
    start = shift_boundary(shift_day, shift_start)
    elapsed = instant - start
    if elapsed <= timedelta(minutes=10):
        return 0
    return max(0, int(elapsed.total_seconds() // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = int((normalize_utc(punch_out) - normalize_utc(punch_in)).total_seconds())
    rounded = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "00:00"
) -> int:
    day = parse_calendar_date(date_str)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = shift_boundary(end_day, shift_end)
    elapsed = normalize_utc(punch_out).astimezone(IST) - end
    minutes = int(elapsed.total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def round_half_up(value: float, decimals: int) -> float:
    """Round a float to `decimals` places using ROUND_HALF_UP."""
    quantize_str = "0." + "0" * decimals
    return float(Decimal(str(value)).quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


def is_working_day(d: date) -> bool:
    """Return True for Mon-Fri (weekday() 0-4)."""
    return d.weekday() < 5


def serialize_employee(employee: dict) -> dict:
    result = {key: value for key, value in employee.items() if key != "_id"}
    result["created_at"] = datetime_to_epoch_ms(result.get("created_at"))
    return result


def serialize_attendance(record: dict) -> dict:
    result = {key: value for key, value in record.items() if key != "_id"}
    result["punch_in"] = datetime_to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = datetime_to_epoch_ms(result.get("punch_out"))
    history = []
    for entry in result.get("history", []):
        converted = dict(entry)
        converted["at"] = datetime_to_epoch_ms(converted.get("at"))
        changes = {}
        for field_name, values in converted.get("changes", {}).items():
            change = dict(values)
            if field_name in {"punch_in", "punch_out"}:
                change["from"] = datetime_to_epoch_ms(change.get("from"))
                change["to"] = datetime_to_epoch_ms(change.get("to"))
            changes[field_name] = change
        converted["changes"] = changes
        history.append(converted)
    result["history"] = history
    return result


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_calendar_date(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for that date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    ts_utc = normalize_utc(ts)

    # Find the most recent open record (any punch_in time, open = punch_out is None)
    # Per spec: 404 if no punch-in found; 422 if punched_at <= punch_in.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "No open punch-in found for this employee")

    punch_in_ts = normalize_utc(record["punch_in"])

    # Validate: punch_out must be strictly after punch_in
    if ts_utc <= punch_in_ts:
        raise HTTPException(422, "punched_at must be after punch_in")

    # Validate: punch_out must be within 24 hours of punch_in
    if (ts_utc - punch_in_ts) > timedelta(hours=24):
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_ts, ts_utc)
    ot = compute_overtime(ts_utc, emp["shift_end"], record["date"], emp["shift_start"])
    hd = wh < 4.50

    # Atomic update: only update if punch_out is still None (prevents duplicate punch-out)
    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record already punched out")

    return serialize_attendance(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            try:
                parse_calendar_date(date_from)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            q["date"]["$gte"] = date_from
        if date_to:
            try:
                parse_calendar_date(date_to)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(item) for item in page_docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # Validate date format
    try:
        att_date = parse_calendar_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # Validate employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    # Fetch current record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    # Resolve new values (None means keep existing)
    new_status = body.status if body.status is not None else record.get("status")
    new_punch_in_ms = body.punch_in
    new_punch_out_ms = body.punch_out

    # Rule: ABSENT or LEAVE must not have punch times supplied
    if new_status in ("ABSENT", "LEAVE"):
        if new_punch_in_ms is not None or new_punch_out_ms is not None:
            raise HTTPException(422, "punch_in and punch_out must be null for ABSENT/LEAVE status")

    # Resolve datetime values
    if new_punch_in_ms is not None:
        new_punch_in_dt = epoch_ms_to_datetime(new_punch_in_ms)
    else:
        # Keep existing (could be naive datetime from MongoDB)
        existing_pi = record.get("punch_in")
        new_punch_in_dt = existing_pi

    if new_punch_out_ms is not None:
        new_punch_out_dt = epoch_ms_to_datetime(new_punch_out_ms)
    else:
        existing_po = record.get("punch_out")
        new_punch_out_dt = existing_po

    # If setting to ABSENT/LEAVE, clear punch times
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in_dt = None
        new_punch_out_dt = None

    # Presence status requires punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        if new_punch_in_dt is None:
            raise HTTPException(422, "punch_in is required for presence status")

    # Validate punch_in stays on the record's attendance date (R1)
    if new_punch_in_dt is not None:
        pin_att_date = attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if pin_att_date != att_date:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")

    # Validate punch_out is after punch_in and within 24 hours
    if new_punch_in_dt is not None and new_punch_out_dt is not None:
        pin_utc = normalize_utc(new_punch_in_dt)
        pout_utc = normalize_utc(new_punch_out_dt)
        if pout_utc <= pin_utc:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (pout_utc - pin_utc) > timedelta(hours=24):
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Compute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    elif new_punch_in_dt is not None:
        new_late_minutes = compute_late_minutes(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if new_punch_out_dt is not None:
            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_overtime_minutes = compute_overtime(new_punch_out_dt, emp["shift_end"], date, emp["shift_start"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
    else:
        new_late_minutes = record.get("late_minutes", 0)
        new_work_hours = record.get("work_hours")
        new_overtime_minutes = record.get("overtime_minutes", 0)
        new_half_day = record.get("half_day", False)

    # Build changes dict (only fields that actually changed)
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    old_punch_in_utc = normalize_utc(old_punch_in) if old_punch_in is not None else None
    old_punch_out_utc = normalize_utc(old_punch_out) if old_punch_out is not None else None
    new_punch_in_utc = normalize_utc(new_punch_in_dt) if new_punch_in_dt is not None else None
    new_punch_out_utc = normalize_utc(new_punch_out_dt) if new_punch_out_dt is not None else None

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in_utc != new_punch_in_utc:
        changes["punch_in"] = {"from": old_punch_in_utc, "to": new_punch_in_utc}
    if old_punch_out_utc != new_punch_out_utc:
        changes["punch_out"] = {"from": old_punch_out_utc, "to": new_punch_out_utc}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, "No fields changed")

    # Build history entry
    now_utc = datetime.now(UTC).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update guarded by current history length to prevent concurrent edit loss
    current_history_len = len(record.get("history", []))
    update_result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_utc,
                "punch_out": new_punch_out_utc,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if update_result is None:
        raise HTTPException(409, "Concurrent update detected; please retry")

    return serialize_attendance(update_result)


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    year, mon = parse_month(month)
    first_day, last_day = month_date_range(year, mon)

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    # Count working days (Mon-Fri) from max(first_day, joined_on) to last_day
    joined_on = emp["joined_on"]
    effective_start_str = max(first_day, joined_on)
    effective_start = date.fromisoformat(effective_start_str)
    last = date.fromisoformat(last_day)

    working_days = 0
    d = effective_start
    while d <= last:
        if is_working_day(d):
            working_days += 1
        d += timedelta(days=1)

    # Aggregate attendance for this employee in the month
    # MongoDB $dayOfWeek: 1=Sunday, 2=Monday, ..., 6=Friday, 7=Saturday
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],  # Mon=2..Fri=6
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        agg = results[0]
        present_days = round_half_up(float(agg["present_days"]), 2)
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = None,
):
    year, mon = parse_month(month)
    first_day, last_day = month_date_range(year, mon)

    # Get headcounts per department (employees joined on or before end of month)
    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    dept_emp_pipeline = [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    dept_employees = list(db.employees.aggregate(dept_emp_pipeline))
    if not dept_employees:
        return {"month": month, "items": []}

    # Get attendance stats grouped by department via $lookup
    att_match: dict = {"date": {"$gte": first_day, "$lte": last_day}}
    att_pipeline = [
        {"$match": att_match},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": {"path": "$emp_info", "preserveNullAndEmptyArrays": False}},
    ]

    if department:
        att_pipeline.append({"$match": {"emp_info.department": department}})

    att_pipeline.append(
        {
            "$group": {
                "_id": "$emp_info.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$type": "$work_hours"}, "null"]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$type": "$work_hours"}, "null"]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        }
    )

    att_results = {r["_id"]: r for r in db.attendance_logs.aggregate(att_pipeline)}

    items = []
    for dept_info in dept_employees:
        dept_name = dept_info["_id"]
        headcount = dept_info["headcount"]
        att = att_results.get(dept_name, {})

        present_days_raw = float(att.get("present_days", 0))
        work_hours_count = int(att.get("work_hours_count", 0))
        work_hours_sum = float(att.get("work_hours_sum", 0))

        avg_work_hours = None
        if work_hours_count > 0:
            avg_work_hours = round_half_up(work_hours_sum / work_hours_count, 2)

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": round_half_up(present_days_raw, 2),
            "avg_work_hours": avg_work_hours,
            "late_count": int(att.get("late_count", 0)),
            "total_late_minutes": int(att.get("total_late_minutes", 0)),
            "leave_count": int(att.get("leave_count", 0)),
            "on_duty_count": int(att.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = parse_month(month)
    first_day, last_day = month_date_range(year, mon)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": {"path": "$emp_info", "preserveNullAndEmptyArrays": False}},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline += [
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp_info.name"},
                "department": {"$first": "$emp_info.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]

    all_results = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: 1, 2, 2, 4 ...
    ranked = []
    current_rank = 1
    for i, row in enumerate(all_results):
        if i > 0 and row["total_late_minutes"] < all_results[i - 1]["total_late_minutes"]:
            current_rank = i + 1
        ranked.append({
            "rank": current_rank,
            "emp_code": row["_id"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        })

    # Keep all rows with rank <= limit (ties at the cutoff are included)
    items = [r for r in ranked if r["rank"] <= limit]

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        from_d = parse_calendar_date(from_date)
        to_d = parse_calendar_date(to_date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if to_d < from_d:
        raise HTTPException(422, "to must be on or after from")

    # > 92 days means (to - from).days > 91, i.e., at least 93 calendar days
    if (to_d - from_d).days > 91:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Check department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    from_str = from_d.isoformat()
    to_str = to_d.isoformat()

    # Get all employees in the department
    emp_list = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))
    emp_codes = [e["emp_code"] for e in emp_list]

    # Get attendance logs for the department in the range
    logs = list(db.attendance_logs.find(
        {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_str, "$lte": to_str},
        },
        {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1}
    ))

    # Build per-day maps
    day_present: dict = {}
    day_late: dict = {}
    for log in logs:
        d_str = log["date"]
        status = log.get("status", "")
        late_minutes = log.get("late_minutes", 0) or 0
        half_day = log.get("half_day", False) or False

        if status in ("PRESENT", "WFH", "ON_DUTY"):
            present_val = 0.5 if half_day else 1.0
            day_present[d_str] = day_present.get(d_str, 0) + present_val

        if late_minutes > 0:
            day_late[d_str] = day_late.get(d_str, 0) + 1

    def headcount_on(day_str: str) -> int:
        return sum(1 for e in emp_list if e["joined_on"] <= day_str)

    items = []
    rate_history = []  # track all rates (including None) for moving avg window

    current = from_d
    while current <= to_d:
        d_str = current.isoformat()
        wd = is_working_day(current)
        hc = headcount_on(d_str)
        present_count = day_present.get(d_str, 0)
        late_count = day_late.get(d_str, 0)

        if wd and hc > 0:
            att_rate = round_half_up(present_count / hc, 4)
        else:
            att_rate = None

        rate_history.append(att_rate)

        # Moving average of up to 7 rows (current + up to 6 preceding), only non-null rates
        window = rate_history[-7:]
        non_null = [r for r in window if r is not None]
        moving_avg = round_half_up(sum(non_null) / len(non_null), 4) if non_null else None

        items.append({
            "date": d_str,
            "is_working_day": wd,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": moving_avg,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


def bson_to_json_safe(doc) -> dict:
    """Convert a BSON document (which may contain non-JSON-serialisable types like
    Timestamp, Binary, ObjectId) into a plain Python dict that FastAPI can encode.
    Uses bson.json_util for the round-trip."""
    import json as _json
    return _json.loads(json_util.dumps(doc))


# --------------------------------------------------------------------------- #
# Admin endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}. Valid values: {sorted(valid_endpoints)}")

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                try:
                    parse_calendar_date(date_from)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                q["date"]["$gte"] = date_from
            if date_to:
                try:
                    parse_calendar_date(date_to)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        _page = int(page)
        _page_size = int(page_size)
        explained = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip((_page - 1) * _page_size).limit(_page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_json_safe(explained)}

    elif endpoint == "employee_monthly":
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        year, mon = parse_month(month)
        first_day, last_day = month_date_range(year, mon)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day, "$lte": last_day},
                }
            },
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        # PyMongo 4.x does not support aggregate(explain=True); use db.command instead
        explained = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline, explain=True, cursor={},
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_json_safe(explained)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        year, mon = parse_month(month)
        first_day, last_day = month_date_range(year, mon)

        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {"$unwind": "$emp_info"},
            {"$group": {"_id": "$emp_info.department", "count": {"$sum": 1}}},
        ]
        explained = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline, explain=True, cursor={},
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_json_safe(explained)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        year, mon = parse_month(month)
        first_day, last_day = month_date_range(year, mon)

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day, "$lte": last_day},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {"$unwind": "$emp_info"},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
            {"$sort": {"total": DESCENDING}},
        ]
        if department:
            pipeline.insert(2, {"$match": {"emp_info.department": department}})

        explained = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline, explain=True, cursor={},
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_json_safe(explained)}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date:
            raise HTTPException(422, "from is required for department_trend")
        if not to_date:
            raise HTTPException(422, "to is required for department_trend")

        try:
            from_d = parse_calendar_date(from_date)
            to_d = parse_calendar_date(to_date)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        from_str = from_d.isoformat()
        to_str = to_d.isoformat()

        # Explain the main attendance_logs find (same query as the actual endpoint)
        emp_list = list(db.employees.find(
            {"department": department},
            {"emp_code": 1, "_id": 0}
        ))
        emp_codes = [e["emp_code"] for e in emp_list]
        explained = db.attendance_logs.find(
            {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        ).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_json_safe(explained)}

    raise HTTPException(422, "Unhandled endpoint")
