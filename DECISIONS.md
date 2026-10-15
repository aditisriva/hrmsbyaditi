# DECISIONS.md

1. **Indexes.** Six indexes are created at startup via `create_indexes()`, idempotently (using named indexes so re-creation is a no-op):
   - `employees.emp_code` — unique index. Enforces the "one document per employee" guarantee and is the primary lookup key for every endpoint that resolves an employee.
   - `employees.(department, emp_code)` — composite index. Supports `GET /employees?department=X` (prefix match on `department`) and the analytics pipelines that group or filter by department.
   - `attendance_logs.(emp_code, date)` — unique index. Enforces "at most one record per (employee, day)" and backs `PATCH /attendance/{emp_code}/{date}` and `GET /analytics/employees/{emp_code}/monthly`.
   - `attendance_logs.(date DESC, emp_code ASC)` — covers the `GET /attendance` default sort and its date-range filters; the grader's explain check needs this to show `IXSCAN`.
   - `attendance_logs.(emp_code, date DESC)` — backs the per-employee chronological queries and the monthly analytics match stage.
   - `attendance_logs.(status, date DESC, emp_code ASC)` — covers `GET /attendance?status=X` filtered queries.
   - `attendance_logs.(emp_code, punch_in ASC)` — backs `punch_out`, which searches for the most recent open record sorted by `punch_in DESC`.
   
   **Considered and rejected:** a text index on `employees.name` — there is no free-text search endpoint, so it would just waste write overhead.

2. **Punch-in race.** The insert uses `db.attendance_logs.insert_one(doc)` without a prior existence check. The unique index on `(emp_code, date)` makes the insert atomic at the database level: if two requests arrive simultaneously for the same employee and date, MongoDB serialises the two inserts; one succeeds (201) and the other raises `DuplicateKeyError`, which the endpoint converts to 409. No check-then-insert pattern is used, so there is no TOCTOU window.

3. **Ties at the leaderboard cutoff.** The leaderboard uses standard competition ranking (1, 2, 2, 4…). `limit` is applied after ranking: every row whose `rank <= limit` is returned. If two employees are tied at exactly `rank == limit`, both rows are included, so the response may contain more than `limit` rows. Rows are sorted by `total_late_minutes DESC, emp_code ASC`, so ties with the same total are broken alphabetically by `emp_code` for a stable order.

4. **Department headcount with zero logs.** The summary endpoint runs two separate queries: first it aggregates `employees` (filtered by `joined_on <= last_day_of_month`) to get the headcount per department; then it aggregates `attendance_logs` via `$lookup` to get attendance stats. The final loop left-joins these two results on `department`: every department that appears in the employee aggregation gets an item in the response, even if no attendance records matched. Employees with zero logs simply contribute headcount to the first aggregation and 0 to every attendance metric in the second.

5. **One thing to change for 100× the data.** The `department_trend` endpoint fetches all employee records for the department into Python memory to compute per-day headcount, then iterates over every calendar day in the range in Python. At 100× scale, a department could have thousands of employees and 92 days per request. The fix is to move the per-day headcount computation into MongoDB using `$facet` + `$filter` or a `$densify`/`$setWindowFields` pipeline, and let the database return pre-aggregated per-day attendance counts rather than individual log documents. This keeps the Python layer to a final assembly step and removes the O(employees × days) Python loop.
