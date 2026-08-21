# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Demo transcripts for the Interview Feedback Agent.

The agent's whole job is to read what was said in an interview. On this bench nothing has
ever been said: there are two Interviews, no transcript anywhere, and no field that used to
hold one -- so every run ends at "no transcript is available" and the agent looks broken when
it is in fact behaving correctly.

`seed()` files one transcript against one Interview, in the shape a real recorder would
produce, and `clear()` puts the Interview back. Four flavours, because the interesting
behaviour is not the happy path:

    full        a complete Technical Round as pasted plain text with timestamps -- the
                happy path; every speaker resolves and the agent should rate and write
    vtt         the same conversation as an attached WebVTT caption file with <v Speaker>
                tags split across cues -- exercises the parser and the attachment source
    anonymous   provider JSON with "Speaker 1"/"Speaker 2" and no names -- the agent should
                REFUSE to rate, because it cannot tell which voice is the candidate
    thin        three lines -- the agent should refuse as too short to assess anyone on

    bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.seed
    bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.seed --kwargs "{'flavour': 'anonymous'}"
    bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.status
    bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.run
    bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.clear

`run()` fires the agent inline and prints the Flow Run, so a whole test is two commands
without opening the browser. Nothing here submits anything, and `clear()` also resets the
ratings and feedback text on the interview's draft Interview Feedback records, so a seeded
test leaves no trace.

The transcripts are invented. They are about a made-up billing service on purpose -- the
point is to give the agent something specific enough that a vague assessment is visibly
wrong.
"""

from __future__ import annotations

import json
import os
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

MANIFEST_FILENAME = "interview_feedback_demo_manifest.json"

# ? FIELDS THE SEEDER TOUCHES ON Interview. Recorded before they are written so clear()
# ? can put back exactly what was there -- normally None, but not worth assuming.
TRANSCRIPT_FIELDS = (
	"custom_transcript_text",
	"custom_transcript_file",
	"custom_transcript_source",
	"custom_transcript_received_on",
	"custom_transcript_external_ref",
)

FULL = """[00:00:04] {interviewer}: Hi {candidate}, thanks for making the time. Start me off -- what have you been building lately?
[00:00:21] {candidate}: For the last year I have owned a Django service that produces our billing exports. The piece I actually own end to end is the nightly reconciliation job -- it reads about two million ledger rows, diffs them against what we invoiced, and files the mismatches for a human to look at.
[00:01:02] {interviewer}: Two million rows a night. How long did that take, and what did you do about it?
[00:01:15] {candidate}: It started at about forty minutes. Most of that was one query doing a row-by-row lookup against the ledger table. I replaced it with a single join, added a composite index on (account, posted_on), and moved the diff itself into a set operation in Postgres instead of pulling rows into Python. It runs in about four minutes now.
[00:02:30] {second_interviewer}: Let me push on that. How do you know the index was what helped, and not the join rewrite?
[00:02:44] {candidate}: I measured them separately. EXPLAIN ANALYZE before and after each change, on a restored copy of production. The join rewrite took forty minutes to about eleven. The index took eleven to four. I kept both because they were fixing different things -- one cut the number of round trips, the other cut the scan.
[00:03:40] {second_interviewer}: What happens when that job dies halfway through?
[00:03:52] {candidate}: It is idempotent per batch. Each batch writes a marker row before it starts and clears it on success, so a restart resumes from the last incomplete batch instead of redoing the whole night. We did lose a night early on, before that existed. That is why it exists.
[00:05:10] {interviewer}: How do you test it?
[00:05:18] {candidate}: Unit tests on the diff logic against hand-built fixtures, and one integration test that loads a small ledger into a test database and runs the job end to end. I will be honest, that integration test is slow and people skip it locally. I have not fixed that.
[00:06:15] {second_interviewer}: If you had to hand this service to someone tomorrow, what would worry you?
[00:06:28] {candidate}: The reconciliation rules live in code, not config, so a finance change means a deploy. And the runbook assumes you know why the marker rows exist. I would write that down before handing it over.
[00:07:20] {second_interviewer}: Same problem from scratch. What would you do differently?
[00:07:31] {candidate}: Batch markers from day one instead of after an incident, and keep the diff in the database from the start rather than learning that the hard way.
[00:08:20] {interviewer}: Anything you want to ask us?
[00:08:27] {candidate}: Yes -- how many engineers would own this service, and would I be on the on-call rotation for it?
[00:08:40] {interviewer}: Six, and yes, a weekly rotation. Thanks {candidate}, we will be in touch this week.
"""

VTT = """WEBVTT

1
00:00:04.000 --> 00:00:12.000
<v {interviewer}>Hi {candidate}, thanks for making the time. Start me off -- what have you

2
00:00:12.100 --> 00:00:15.000
<v {interviewer}>been building lately?

3
00:00:16.000 --> 00:00:29.000
<v {candidate}>For the last year I have owned a Django service that produces our billing

4
00:00:29.100 --> 00:00:41.000
<v {candidate}>exports. The piece I own end to end is the nightly reconciliation job -- it reads about

5
00:00:41.100 --> 00:00:52.000
<v {candidate}>two million ledger rows, diffs them against what we invoiced, and files the mismatches.

6
00:01:02.000 --> 00:01:09.000
<v {interviewer}>Two million rows a night. How long did that take, and what did you do about it?

7
00:01:15.000 --> 00:01:31.000
<v {candidate}>It started at about forty minutes. Most of it was one query doing a row-by-row lookup

8
00:01:31.100 --> 00:01:48.000
<v {candidate}>against the ledger. I replaced it with a single join, added a composite index on account
and posted_on, and moved the diff into a set operation in Postgres. It runs in four minutes now.

9
00:02:30.000 --> 00:02:38.000
<v {second_interviewer}>How do you know the index was what helped, and not the join rewrite?

10
00:02:44.000 --> 00:03:02.000
<v {candidate}>I measured them separately, with EXPLAIN ANALYZE before and after each change on a
restored copy of production. The join took forty minutes to eleven. The index took eleven to four.

11
00:03:40.000 --> 00:03:46.000
<v {second_interviewer}>What happens when that job dies halfway through?

12
00:03:52.000 --> 00:04:10.000
<v {candidate}>It is idempotent per batch. Each batch writes a marker row before it starts and clears
it on success, so a restart resumes from the last incomplete batch. We lost a night before that existed.

13
00:05:18.000 --> 00:05:34.000
<v {candidate}>Unit tests on the diff logic, and one integration test that runs the job end to end.
That one is slow and people skip it locally. I have not fixed that.
"""

# ? THE SAME CONVERSATION WITH THE NAMES STRIPPED, IN THE SHAPE A MEETING API RETURNS.
# ? The agent must not guess which numbered speaker is the candidate.
ANONYMOUS: dict[str, Any] = {
	"meeting": {"id": "19:demo_meeting_0001", "subject": "Technical Round"},
	"data": {
		"transcript": {
			"segments": [
				{"speakerName": "Speaker 1", "startTime": 4.0, "text": "Start me off -- what have you been building lately?"},
				{"speakerName": "Speaker 2", "startTime": 21.0, "text": "For the last year I have owned a Django service that produces our billing exports. The piece I own end to end is the nightly reconciliation job -- about two million ledger rows a night, diffed against what we invoiced."},
				{"speakerName": "Speaker 1", "startTime": 62.0, "text": "Two million rows a night. How long did that take, and what did you do about it?"},
				{"speakerName": "Speaker 2", "startTime": 75.0, "text": "It started at about forty minutes. I replaced a row-by-row lookup with a single join, added a composite index on account and posted_on, and moved the diff into a set operation in Postgres. It runs in about four minutes now."},
				{"speakerName": "Speaker 1", "startTime": 150.0, "text": "How do you know the index was what helped, and not the join rewrite?"},
				{"speakerName": "Speaker 2", "startTime": 164.0, "text": "I measured them separately with EXPLAIN ANALYZE on a restored copy of production. The join rewrite took forty minutes to eleven, the index took eleven to four."},
				{"speakerName": "Speaker 1", "startTime": 220.0, "text": "What happens when that job dies halfway through?"},
				{"speakerName": "Speaker 2", "startTime": 232.0, "text": "It is idempotent per batch. Each batch writes a marker row before it starts and clears it on success, so a restart resumes from the last incomplete batch."},
				{"speakerName": "Speaker 1", "startTime": 310.0, "text": "How do you test it?"},
				{"speakerName": "Speaker 2", "startTime": 318.0, "text": "Unit tests on the diff logic, and one integration test that runs the job end to end. It is slow and people skip it locally. I have not fixed that."},
			]
		}
	},
}

THIN = """[00:00:03] {interviewer}: Thanks for joining. Shall we start?
[00:00:07] {candidate}: Yes, happy to.
[00:00:11] {interviewer}: Sorry -- my next meeting has been moved. Can we reschedule?
"""

FLAVOURS = ("full", "vtt", "anonymous", "thin")


# ==============================================================================
# MANIFEST
# ==============================================================================


def _manifest_path() -> str:
	return os.path.join(frappe.get_site_path("private", "files"), MANIFEST_FILENAME)


def _load_manifest() -> dict[str, Any]:
	path = _manifest_path()
	if not os.path.exists(path):
		return {}
	with open(path) as f:
		return json.load(f)


def _save_manifest(manifest: dict[str, Any]) -> None:
	with open(_manifest_path(), "w") as f:
		json.dump(manifest, f, indent=2, default=str)


def _drop_manifest() -> None:
	path = _manifest_path()
	if os.path.exists(path):
		os.remove(path)


# ==============================================================================
# SEED
# ==============================================================================


def seed(flavour: str = "full", interview: str | None = None) -> dict[str, Any]:
	"""File one demo transcript against one Interview. Re-seeding clears the previous one first."""
	if flavour not in FLAVOURS:
		frappe.throw(f"flavour must be one of {', '.join(FLAVOURS)}")

	if _load_manifest():
		print("A demo transcript is already seeded; clearing it first.")
		clear()

	doc = frappe.get_doc("Interview", interview or _pick_interview())
	names = _names(doc)
	manifest: dict[str, Any] = {
		"interview": doc.name,
		"flavour": flavour,
		"seeded_at": str(now_datetime()),
		"previous": {f: doc.get(f) for f in TRANSCRIPT_FIELDS},
		"files": [],
	}

	if flavour == "vtt":
		attachment = _attach(doc, "demo-interview-transcript.vtt", VTT.format(**names))
		manifest["files"].append(attachment.name)
		detail = f"attached {attachment.file_name}"
	elif flavour == "anonymous":
		payload = json.dumps(_anonymous_payload(), indent=2)
		attachment = _attach(doc, "demo-meeting-transcript.json", payload)
		manifest["files"].append(attachment.name)
		doc.db_set("custom_transcript_external_ref", "19:demo_meeting_0001", update_modified=False)
		detail = f"attached {attachment.file_name} (speakers unnamed on purpose)"
	else:
		text = (THIN if flavour == "thin" else FULL).format(**names)
		doc.db_set("custom_transcript_text", text, update_modified=False)
		detail = f"{len(text)} characters of text on the Interview"

	doc.db_set("custom_transcript_source", f"Demo seeder ({flavour})", update_modified=False)
	doc.db_set("custom_transcript_received_on", now_datetime(), update_modified=False)

	_save_manifest(manifest)
	frappe.db.commit()

	expectation = {
		"full": "the agent should rate the rubric and write a draft",
		"vtt": "the agent should rate the rubric and write a draft, reading the caption file",
		"anonymous": "the agent should REFUSE to rate -- it cannot tell which speaker is the candidate",
		"thin": "the agent should REFUSE -- the transcript is too short to assess anyone on",
	}[flavour]

	result = {
		"interview": doc.name,
		"candidate": names["candidate"],
		"flavour": flavour,
		"seeded": detail,
		"expect": expectation,
		"next": [
			f"open Interview {doc.name} and use Actions > Draft Feedback from Transcript, or",
			"bench --site frappe.localhost execute flow.flow_hr.demo.interview_feedback_demo.run",
		],
	}
	print(frappe.as_json(result, indent=2))
	return result


def _pick_interview() -> str:
	"""The most recently scheduled Interview that has a panel and an open draft feedback --
	without those the agent has nobody to write for."""
	rows = frappe.get_all(
		"Interview",
		filters={"docstatus": ("!=", 2)},
		fields=["name", "interview_type", "job_applicant"],
		order_by="scheduled_on desc, creation desc",
	)
	for row in rows:
		has_panel = frappe.db.exists("Interview Detail", {"parent": row.name, "interviewer": ("is", "set")})
		has_draft = frappe.db.exists("Interview Feedback", {"interview": row.name, "docstatus": 0})
		if has_panel and has_draft:
			return row.name
	if rows:
		print(
			f"No Interview has both a panel and a draft feedback; using {rows[0].name}. The agent "
			"will fall back to commenting rather than filling a feedback record."
		)
		return rows[0].name
	frappe.throw("There are no Interviews on this site to seed a transcript against.")


def _names(doc) -> dict[str, str]:
	panel = frappe.get_all(
		"Interview Detail",
		filters={"parent": doc.name},
		fields=["interviewer", "custom_interviewer_name"],
		order_by="idx",
	)
	labels = [
		(row.custom_interviewer_name or frappe.db.get_value("User", row.interviewer, "full_name") or row.interviewer)
		for row in panel
		if row.interviewer
	] or ["Interviewer"]

	candidate = (
		frappe.db.get_value("Job Applicant", doc.job_applicant, "applicant_name")
		or doc.get("custom_applicant_name")
		or "Candidate"
	)
	return {
		"interviewer": labels[0],
		"second_interviewer": labels[1] if len(labels) > 1 else labels[0],
		"candidate": candidate,
	}


def _anonymous_payload() -> dict[str, Any]:
	return json.loads(json.dumps(ANONYMOUS))


def _attach(doc, file_name: str, content: str):
	return frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": "Interview",
			"attached_to_name": doc.name,
			"content": content,
			"is_private": 1,
		}
	).insert(ignore_permissions=True)


# ==============================================================================
# RUN
# ==============================================================================


def run(interview: str | None = None) -> dict[str, Any]:
	"""Fire the agent inline against the seeded Interview and print the Flow Run.

	Inline, not queued, so the run is finished when this returns -- a queued run needs a
	worker and gives nothing back to read.
	"""
	manifest = _load_manifest()
	interview = interview or manifest.get("interview")
	if not interview:
		frappe.throw("Nothing is seeded and no interview was passed. Run seed() first.")

	from flow.flow_hr.interview_feedback_agent import _settings_doc
	from flow.triggers import fire_manual

	settings = _settings_doc()
	run_name = fire_manual(
		settings.trigger, target_doctype="Interview", target_name=interview, enqueue=False
	)
	flow_run = frappe.get_doc("Flow Run", run_name)

	print("=" * 78)
	print(f"Flow Run {run_name} -- {flow_run.status} in {flow_run.iterations} iterations")
	print(f"write mode: {settings.write_mode}")
	print("=" * 78)
	print(flow_run.output or flow_run.error or "(no output)")
	print("=" * 78)

	feedback = frappe.get_all(
		"Interview Feedback",
		filters={"interview": interview},
		fields=["name", "interviewer", "result", "docstatus", "custom_obtained_average_score"],
	)
	for row in feedback:
		print(f"  {row.name}  {row.interviewer}  result={row.result}  "
			f"score={row.custom_obtained_average_score}  docstatus={row.docstatus}")
	comments = frappe.get_all(
		"Comment",
		filters={"reference_doctype": "Interview", "reference_name": interview, "comment_type": "Comment"},
		pluck="name",
	)
	print(f"  comments on the Interview: {len(comments)}")

	return {
		"run": run_name,
		"status": flow_run.status,
		"iterations": flow_run.iterations,
		"feedback": feedback,
		"comments": comments,
	}


# ==============================================================================
# STATUS / CLEAR
# ==============================================================================


def status() -> dict[str, Any]:
	"""What is seeded, and what the resolver can currently see."""
	manifest = _load_manifest()
	if not manifest:
		print("Nothing seeded.")
		return {}

	from flow.flow_hr.tools.interview_feedback import interview_transcript

	found = interview_transcript(interview=manifest["interview"])
	transcript = found.get("transcript") or {}
	state = {
		"interview": manifest["interview"],
		"flavour": manifest["flavour"],
		"seeded_at": manifest["seeded_at"],
		"files": manifest["files"],
		"resolver_finds": found.get("found"),
		"source": transcript.get("source"),
		"format": transcript.get("format"),
		"chars": transcript.get("total_chars"),
		"turns": transcript.get("turns"),
		"usable": transcript.get("usable"),
		"speakers": [(s["label"], s["role"]) for s in transcript.get("speakers") or []],
		"warnings": transcript.get("warnings"),
	}
	print(frappe.as_json(state, indent=2))
	return state


def clear() -> dict[str, Any]:
	"""Put the Interview back, and reset the drafts the agent may have written into."""
	manifest = _load_manifest()
	if not manifest:
		print("Nothing seeded.")
		return {}

	interview = manifest["interview"]
	undone: dict[str, Any] = {"interview": interview, "files_deleted": [], "feedback_reset": []}

	for name in manifest.get("files") or []:
		if frappe.db.exists("File", name):
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)
			undone["files_deleted"].append(name)

	for field, previous in (manifest.get("previous") or {}).items():
		frappe.db.set_value("Interview", interview, field, previous, update_modified=False)

	# ? The agent's write is the point of the demo, so undo it too -- but only on drafts.
	# ? A submitted feedback is somebody's decision and is left exactly where it is.
	for name in frappe.get_all(
		"Interview Feedback", filters={"interview": interview, "docstatus": 0}, pluck="name"
	):
		doc = frappe.get_doc("Interview Feedback", name)
		if not (doc.feedback or any(cint(r.custom_rating_given) for r in doc.skill_assessment)):
			continue
		for row in doc.skill_assessment:
			row.custom_rating_given = 0
			row.rating = 0
		doc.feedback = None
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
		doc.db_set("custom_obtained_average_score", 0, update_modified=False)
		undone["feedback_reset"].append(name)

	_drop_manifest()
	frappe.db.commit()
	print(frappe.as_json(undone, indent=2))
	print(
		"Comments the agent left on the Interview are NOT deleted -- they are a readable "
		"record of the run. Remove them from the Interview's timeline if you want them gone."
	)
	return undone
