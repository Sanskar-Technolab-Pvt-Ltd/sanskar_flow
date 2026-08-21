# Flow tools

Python functions exposed to Flow agents as Flow Tool rows of type **Imported**.

| Tool | Import path | Used by | Writes? |
|---|---|---|---|
| `preview_salary_slip` | `flow.flow_hr.tools.payroll.preview_salary_slip` | Payroll Agent | no |
| `leave_impact_report` | `flow.flow_hr.tools.attendance.leave_impact_report` | Attendance Agent | no |
| `attendance_day_context` | `flow.flow_hr.tools.attendance.attendance_day_context` | Attendance Agent | no |
| `interview_transcript` | `flow.flow_hr.tools.interview_feedback.interview_transcript` | Interview Feedback Agent | no |
| `interview_feedback_context` | `flow.flow_hr.tools.interview_feedback.interview_feedback_context` | Interview Feedback Agent | no |
| `record_interview_feedback` | `flow.flow_hr.tools.interview_feedback.record_interview_feedback` | Interview Feedback Agent | **yes** |

## Registering one

The Flow Tool row is what the model actually sees, and `flow.lib.resolver._build_tool`
takes the description from **the row**, not from the function's docstring. The argument
schema is derived from the signature, so every parameter needs an
`Annotated[type, "what this is"]` hint — that string becomes the parameter description
in the tool schema.

```python
frappe.get_doc({
    "doctype": "Flow Tool",
    "slug": "leave_impact_report",          # the name the model calls
    "title": "Leave Impact Report",
    "type": "Imported",
    "import_path": "flow.flow_hr.tools.attendance.leave_impact_report",
    "enabled": 1,
    "requires_confirmation": 0,             # read-only, so nothing to approve
    "description": "...",                   # what the model reads — write it properly
}).insert()
```

Then add the slug to the agent's `tools` table.

## Why these are read-only

Flow's `execute` sandbox is narrow on purpose (no imports, no `frappe.db.sql`, no
`frappe.get_all`, no `doc.insert()`), and a weak model burns iterations rediscovering
that. Anything that needs a real query, a join across doctypes, or arithmetic over a
date range belongs in a tool like these, where it runs once, correctly, server-side.
Writes normally stay with the built-in `create` / `update` / `run_action` tools so they keep
going through the normal permission and confirmation path. `record_interview_feedback` is the
one exception, and the section on it below says why.

## attendance.py

`leave_impact_report` answers "what does approving this leave actually cost?" in one
call: predicted blockers (things this install's validations would refuse), concerns
(real conflicts a human must weigh), notes (background), the at-risk tasks and
milestones with their deadlines, the affected projects and their managers, day-by-day
team coverage, the leave balance, and the addresses to notify.

`attendance_day_context` answers "what attendance is missing, and for which dates is a
request still allowed?" — per date: holiday/week-off, existing Attendance, covering
leave, existing Attendance Request, check-in punches, mispunch, and gap.

Neither decides anything. The verdict is advice; the agent's instructions own the policy.

## interview_feedback.py

`interview_transcript` answers "what was actually said in this interview?" whoever recorded
it. The sources are configuration, not code: `Interview Feedback Agent Settings.transcript_sources`
is an ordered list of places to look — a field on the Interview, an attachment on the
Interview or the Job Applicant, a Python method, an HTTP endpoint — tried top to bottom until
one returns text. WebVTT, SRT, provider JSON and plain text all come back as the same list of
turns, with each speaker labelled Interviewer, Candidate, or `null` when the role could not be
established from the panel, the applicant, or the row's speaker map. A long transcript is
windowed with a `next_offset` cursor. Nothing about Teams, Zoom, Meet or any note-taker is
hard-coded — a new provider is a new row, or at most a new function behind a `Python Method` row.

`interview_feedback_context` answers "what am I allowed to rate, and what is already here?" —
the Expected Skill Set for the round with each skill's own rating scale, the candidate, the
panel, and the Interview Feedback records already waiting (one draft per interviewer is created
automatically when the panel is set, so drafts existing is the normal state, not an error).

`record_interview_feedback` is the only write, and the only tool here that is not read-only.
It exists instead of the built-in `create` / `update` pair because of two traps on this install:
`prompt_hr.py.interview_feedback.on_update` computes the obtained score as
`sum(custom_rating_given) / sum(custom_rating_scale) * 10`, so a skill row saved with a blank
scale is a **ZeroDivisionError on save**, and `interview_feedback.js` refuses a rating above its
row's scale. Handing a model the whole `skill_assessment` table through `update` puts it one
slip away from either. This tool takes ratings by skill name, matches them onto the rows that
already exist, backfills a missing scale from the settings default, and refuses the entire write
— saving nothing — if a skill is off-rubric, rated twice, out of range, or unevidenced.

It never submits. Per `Interview Feedback Agent Settings.write_mode` it either comments on the
Interview (`Suggest Only`, the shipped default, which changes no field anywhere) or fills the
interviewers' draft Interview Feedback and leaves them unsubmitted. Submitting a feedback moves
the Interview's status and then the Job Applicant's, so it stays a human's decision.

Entry points live in `flow.flow_hr.interview_feedback_agent`: `get_transcript_state` for the
button on the Interview form, `generate_feedback_from_transcript` to fire the Manual trigger,
and `ingest_transcript` for a recorder to file a transcript from outside and optionally start
the run. Install everything with
`bench --site <site> execute flow.flow_hr.install.install`.
