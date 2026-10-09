# Review notes

The original Git baseline is not present in this workspace, so an exact starter-to-final diff cannot be reconstructed.
These are the concrete defects found during the implementation audit and the corresponding changes in the current code.

| # | Where | Defect and symptom | Current correction |
|---|---|---|---|
| 1 | `compute_late_minutes`, `compute_overtime`, punch endpoints | Milliseconds affected boundary calculations; overtime was anchored to the punch-out date. A 09:40:00.900 punch counted as late, and a 23:00 punch on a 22:00-06:00 shift appeared to have 17 hours of overtime. | Truncate instants to whole seconds and calculate the shift end from the attendance date, advancing one day for an overnight shift. |
| 2 | `/attendance/punch-out` | A read followed by an `_id`-only update let simultaneous punch-outs both succeed. | Update only when the stored punch-out remains null and the punch-in still matches; return 409 when the conditional write loses. |
| 3 | `regularize_attendance` | Replacing a stale full record could lose concurrent history entries; punch timestamps in history were stored as integers. | Compare the observed state atomically, append with `$push`, retry a bounded number of times, and store history instants as BSON datetimes. |
| 4 | `/attendance` | The handler loaded every matching record before slicing, which is not suitable for the specified 100k-record dataset. | Count the filtered set and perform sorting, skipping, and limiting in MongoDB. |
| 5 | Monthly and department summaries | Weekend attendance was counted as present days; departments with employees but no logs were missing. | Aggregate from employees, count present days only Monday-Friday, and preserve employee groups with empty attendance lookups. |
| 6 | Late leaderboard | Competition ranks were calculated in Python rather than MongoDB. | Use `$setWindowFields` with `$rank`, then sort tied rows by employee code and apply the rank cutoff. |
| 7 | Department trend | `$documents` was used with a collection aggregation, while gap filling, headcount, and moving averages were computed in Python. | Use database-level aggregation, `$densify`, indexed lookups, and `$setWindowFields` for the moving average. |
| 8 | `/admin/explain/{endpoint}` | Aggregation cursors do not expose `.explain()` in the installed PyMongo API; several explain pipelines also diverged from their handlers. | Build explain commands with `executionStats` from the same query/pipeline builders used by the endpoints. |
| 9 | Input validation and attendance serialization | Malformed dates/statuses could reach runtime parsing; legacy rows could omit schema-required `history` and `half_day`. | Validate calendar values and enums as requests enter the API, and serialize legacy defaults without exposing internal fields. |

Other reviewed behavior retained: employee pagination is one-based with filtered totals; the unique employee and attendance indexes support their uniqueness guarantees; punch-in and punch-out do not write manual correction history.
