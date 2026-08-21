# Demo data

## attendance_demo

Creates the collisions the Attendance Agent exists to catch. On a fresh bench none of
them exist, so every impact report comes back clean and the agent looks like it is doing
nothing:

| Gap on a fresh bench | Consequence |
|---|---|
| 1 submitted Leave Allocation for 74 employees | every non-LWP balance reads 0, so nothing can ever be approved |
| every open Task's deadline already in the past | no task is ever "due during the leave" |
| no Leave Block List | no date is ever blocked |
| nobody has future leave | team coverage is always 100% |
| leave-type rule fields all unset | the validations that read them never fire |

### Use

```bash
bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.seed --kwargs "{'dry_run': 1}"
bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.seed
bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.status
bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.clear
```

`seed()` picks a cohort of ~14 Active employees in `Development - STPL` who share a
reporting manager (so immediate-team coverage actually moves) and who have open tasks
whose deadlines can legally be pushed forward. Employees carrying a relieving or
resignation date are skipped — they make a confusing demo subject.

It then creates leave allocations, shift assignments and check-ins; pushes a handful of
each person's task deadlines into a window ~18 days out, promoting one to a
High-priority milestone inside that window; puts three of the cohort's peers on approved
leave across the same three days; adds a five-day release freeze as a Leave Block List
linked to the department; and switches on the leave-type and HR Settings rules that
were sitting unset.

### Reversibility

Everything is tracked in `private/files/attendance_demo_manifest.json`: records created
by name, and the previous value of every field changed. `clear()` restores the fields
first, then deletes the records (cancelling submitted ones on the way), and removes the
manifest only if nothing failed. `status()` reports what the manifest claims and whether
it is still there.

Seeding runs with `frappe.flags.in_migrate` set, because `flow.triggers.dispatch`
short-circuits on that flag — otherwise each seeded Leave Application would fire the
Attendance Agent once.

### Not covered by the manifest

Leave Applications filed *by hand* to demo the agent are not seeder output, and `clear()`
deliberately will not touch them:

| Record | State | What it demonstrates |
|---|---|---|
| `HR-LAP-2026-00040` | Pending / Open | `needs_review` — agent left it pending, wrote the project sign-off rows, escalated by email |
| `HR-LAP-2026-00041` | Approved / submitted | `clean` — agent set status then ran Approve, and notified |

They are left out on purpose. Setting a Leave Application's `workflow_state` to
`"Cancelled"` on this install **deletes the record** (`prompt_hr.py.leave_application.
before_validate`), and an approved one has already written Leave Ledger Entries and
Attendance rows, so unwinding it is a decision for a person, not a cleanup script.
Remove them by hand if you want the bench back exactly as it was.

## interview_feedback_demo

Gives the Interview Feedback Agent something to read. On a fresh bench there is no transcript
anywhere and no field that ever held one, so every run correctly ends at "no transcript is
available" — which looks like a broken agent rather than a working one.

`seed()` files one transcript against one Interview in the shape a real recorder produces, and
picks the Interview itself: the most recently scheduled one that has both a panel and an open
draft Interview Feedback, because without those the agent has nobody to write for.

| Flavour | What it files | What the agent should do |
|---|---|---|
| `full` (default) | a complete Technical Round as pasted plain text with timestamps | rate the rubric and write a draft |
| `vtt` | the same conversation as an attached WebVTT caption file, `<v Speaker>` tags split across cues | the same, reading the attachment |
| `anonymous` | provider JSON with `Speaker 1` / `Speaker 2` and no names | **refuse to rate** — it cannot tell which voice is the candidate — and record that as Inconclusive |
| `thin` | three lines, an interview that never happened | **refuse** — too short to assess anyone on |

### Use

```bash
bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.seed
bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.seed --kwargs "{'flavour': 'anonymous'}"
bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.status
bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.run
bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.clear
```

`status()` reports what is seeded *and* what the resolver actually finds — the source it picked,
the format it read, the turn count, and how each speaker was labelled. That is the cheap check:
if `status()` shows the right speakers, the agent will too, and no model call was spent.

`run()` fires the Manual trigger **inline** and prints the Flow Run's output, the feedback
records and the comment count, so a full test is two commands and no browser. Queued runs need
a worker and give nothing back to read. The other way to run it is the form itself:
**Interview → Actions → Draft Feedback from Transcript**.

Re-seeding clears the previous flavour first, so switching flavours is one command.

### Reversibility

`private/files/interview_feedback_demo_manifest.json` records the Interview, the flavour, the
files created, and the previous value of every transcript field. `clear()` restores the fields,
deletes the seeded files, and resets ratings and feedback text on the interview's **draft**
Interview Feedback records — so the agent's write is undone too. A submitted feedback is never
touched: that is somebody's decision, not seeder output.

Comments the agent left on the Interview are deliberately **not** deleted. They are the readable
record of what the agent said, and in `Suggest Only` mode they are the only output there is.

