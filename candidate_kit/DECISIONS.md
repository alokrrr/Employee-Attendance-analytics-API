# Decisions

1. **Indexes.** I create a unique index on `employees.emp_code`, a unique compound index on attendance `(emp_code, date)`, and supporting compound indexes for employee department/join-date lookups and attendance date/status listing and analytics. The unique indexes enforce identity and one record per employee-date; the others match the service’s filters and sort order. I avoided indexing every derived metric because the API does not filter on them and extra indexes increase write cost.

2. **Punch-in race.** Both requests may pass the employee lookup, but each inserts the attendance record directly. MongoDB’s unique `(emp_code, date)` index allows one insert; the other raises a duplicate-key error and receives 409.

3. **Ties.** The aggregation assigns competition rank with `$rank` over total late minutes. If the limit is 2 and ranks are 1, 2, 2, 4, all three employees at ranks 1 and 2 are returned. Tied rows are ordered by `emp_code`.

4. **Headcount.** Department summary starts from eligible employee documents (`joined_on` on or before month end), looks up each employee’s monthly logs, then groups by department. An employee with no logs still contributes one to headcount; a future joiner is excluded.

5. **100x data.** I would first measure the query plans and hot paths, then add materialized monthly analytics if aggregation cost justified it. That keeps the current collections authoritative while reducing repeated work.
