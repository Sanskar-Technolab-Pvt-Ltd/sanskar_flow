# Flow HR — Interview Skill Score Agent (raw tool code)

Just the three Flow Tools the Interview Skill Score Agent calls, as plain Python:

```
flow_hr/
  tools/interview_skill_score.py    the three tools this agent uses
```

`interview_skill_score_context` decides which compulsory skills on a **PMS Job Applicant
Skill Review** still need a row (the ones a live-call agent did not already rate),
`interview_skill_score_transcript` reads the transcript off the linked **Sanskar Interview
Schedule**, and `record_skill_scores` writes ratings onto the review.

No install script and no fixtures here — the Flow Agent, its Flow Tool rows, its trigger and
its knowledge base ("PMS Communication Skill Rating Guidance", with transcript-grounded rating
rubrics for English Speaking & Communication and Client Communication, the two compulsory
skills hardest to judge well from a transcript alone) are set up by hand on the target site
instead of synced from a fixture file. This folder is just the code those Flow Tool rows'
`import_path` points at.

The rest of this folder (`tools/attendance.py`, `tools/payroll.py`,
`tools/interview_feedback.py`, `interview_feedback_agent.py`, `offboarding.py`, `demo/`,
`doctype/`, `vendor/`) belongs to other agents from the full HR agent pack and is not
referenced by anything on this branch — left in place rather than deleted, but inert here.

## What the tools depend on

- **sanskar_erp's PMS module** — for `PMS Job Applicant Skill Review` and
  `PMS Skill Master`/`PMS Skill Weight Item` (where a skill's compulsory-ness is configured).
- **sanskar_interview_agent** — the Sanskar Interview Schedule and its transcript is what
  `interview_skill_score_transcript` reads.
