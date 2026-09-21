# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Flow tools for the Interview Skill Score Agent.

Registered as Flow Tool rows of type "Imported":
    interview_skill_score_context -> flow.flow_hr.tools.interview_skill_score.interview_skill_score_context
    interview_skill_score_transcript -> flow.flow_hr.tools.interview_skill_score.interview_skill_score_transcript
    record_skill_scores -> flow.flow_hr.tools.interview_skill_score.record_skill_scores

This is a different subsystem from the Interview Feedback Agent's tools in
`interview_feedback.py`, which rate the stock hrms Interview / Interview Feedback doctypes
against an Interview Type's Expected Skill Set. This agent instead rates the compulsory
skills configured in PMS (`PMS Skill Weight Item`, via `get_score_weight_map`) and writes
them straight onto a `PMS Job Applicant Skill Review` (app `sanskar_pms`) -- that is the
agent's TARGET doctype, and its Flow Trigger fires with one of those as `doc`.

THE REVIEW AND THE TRANSCRIPT LIVE IN DIFFERENT PLACES. The review is scored against, but the
transcript it is scored FROM sits on `Sanskar Interview Schedule.transcript_json` (app
sanskar_interview_agent) -- the doc the LiveKit call agent actually wrote to. The two are
linked by `interview` (a hrms Interview name), which both doctypes carry, so every tool here
resolves schedule-from-review by that field rather than assuming a name pattern. Getting (or
drafting) the review itself is `ensure_skill_review` in `sanskar_interview_agent.api.results`,
called from `SanskarInterviewSchedule.score_skills()` BEFORE this agent ever runs -- these
tools always receive a review that already exists.

THIS AGENT SCORES THE SKILLS THE INTERVIEW DID NOT REACH. The ones it did reach are already
rated before this agent runs: `Sanskar Interview Question.rating` is filled per spoken question
by the LiveKit call agent, and `seed_ratings_from_schedule` (in `sanskar_interview_agent.api
.results`, called from `SanskarInterviewSchedule.score_skills()`) maps those onto the review's
`ratings` table. A rating made by the scorer that heard the answer beats one reconstructed from
an ASR transcript of that same answer, so those rows arrive settled and show up here as
`already_scored`. `Sanskar Interview Question.rating` itself is never written from this side.

EVERY COMPULSORY SKILL ENDS WITH A VERDICT, BUT NOT ALL OF THEM FROM HERE. Unlike the generic
Interview Feedback Agent (where a skill the interview never covered is simply left `unrated`),
nothing may be left out overall: every compulsory skill for the review's designation gets a row,
either a 1-4 rating or Not Applicable. What this agent owns is `missing` -- the compulsory skills
with no row yet -- which it judges from the transcript, marking Not Applicable where the
conversation never engaged the skill at all rather than inventing a number for it. Any OTHER row
already on the review (a seeded one, or a human's own extra skill) is left exactly as it is.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

import frappe
from frappe import _
from frappe.utils import cint

DEFAULT_MAX_CHARS = 60000

# ? A rating whose own justification says the transcript never touched the skill is not a
# ? rating -- it is a mismarked NA. Seen in practice: "ERPNext -- Accounting: No discussion of
# ? GST settings..." rated 1 (Needs Improvement) instead of not_applicable. "No evidence"/"not
# ? discussed" is a report of silence, and PMS already has a field for silence -- this is a
# ? backstop for the model contradicting itself, not a judgement call this code makes on its
# ? own: it never invents or reclassifies a rating, it only refuses one caught doing this.
#
# Deliberately narrow, and deliberately does NOT include phrases like "admitted no knowledge"
# or "candidate said they didn't know" -- those describe an ANSWER (the skill was raised and
# the candidate showed they lack it), which is genuine evidence for a low rating, not NA. Only
# phrases claiming the transcript never engaged the topic AT ALL belong here.
_NO_EVIDENCE_PHRASES = re.compile(
	r"\b("
	r"no evidence|no discussion (of|about)|no mention (of|was made)|not discussed|"
	r"was not discussed|wasn't discussed|not mentioned|not addressed|not covered|"
	r"never covered|not touched (on|upon)?|not raised|not referenced|not brought up|"
	r"never discussed|never mentioned|never addressed|never raised|never came up|"
	r"didn't come up|did not come up|"
	r"nothing (in the transcript|was said|was discussed|came up)( (about|regarding))?"
	r")\b",
	re.IGNORECASE,
)


def _key(value: Any) -> str:
	"""Normalise a skill name for matching, not for storage.

	A model reproducing a skill name from earlier in its own context routinely swaps an em
	dash for an en dash or a plain hyphen, or drifts on spacing and case -- the words are
	right, the punctuation is not. Matching on this collapsed key (and always writing back the
	CANONICAL name from PMS, never the model's spelling) means that drift never silently
	surfaces as "skill not on the compulsory list", which is otherwise indistinguishable from
	the model naming something that genuinely is not compulsory.
	"""
	return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _find_schedule(review) -> str | None:
	if not review.interview:
		return None
	return frappe.db.get_value("Sanskar Interview Schedule", {"interview": review.interview}, "name")


# ==============================================================================
# TOOL 1 -- THE COMPULSORY SKILLS TO SCORE
# ==============================================================================


def interview_skill_score_context(
	review: Annotated[str, "PMS Job Applicant Skill Review ID, e.g. PMS-JA-REV-2026-00007."],
) -> dict[str, Any]:
	"""Everything needed to score one review's compulsory skills: the skill list, what each
	one means, and any ratings already sitting on the review from an earlier run."""
	frappe.has_permission("PMS Job Applicant Skill Review", "read", review, throw=True)
	doc = frappe.get_doc("PMS Job Applicant Skill Review", review)

	from sanskar_erp.sanskar_pms.doctype.pms_job_applicant_skill_review.pms_job_applicant_skill_review import (
		get_score_weight_map,
	)

	designation = doc.appraisal_designation
	weight_map = get_score_weight_map(designation) if designation else {}
	compulsory_names = [skill for skill, meta in weight_map.items() if meta.get("is_compulsory")]

	skills: list[dict[str, Any]] = []
	if compulsory_names:
		rows = frappe.get_all(
			"PMS Skill Master",
			filters={"name": ("in", compulsory_names)},
			fields=["name", "skill_name", "skill_category", "description"],
		)
		by_name = {row.name: row for row in rows}
		for name in compulsory_names:
			row = by_name.get(name)
			skills.append(
				{
					"skill": name,
					"skill_label": (row.skill_name if row else None) or name,
					"skill_category": (row.skill_category if row else None) or "",
					"what_it_means": (row.description if row else None) or "",
				}
			)

	# ? Mostly NOT leftovers from a previous run of this agent. Before it fires, the schedule
	# ? seeds this table with the skills the interview actually questioned the candidate on,
	# ? carrying across the rating the live call agent gave while it was listening to the answer
	# ? (`seed_ratings_from_schedule` in sanskar_interview_agent.api.results). Those are better
	# ? evidence than anything recoverable here -- this agent reads an ASR transcript of the same
	# ? answers that scorer heard directly -- so they are reported as settled, and `missing` below
	# ? is what is genuinely left to judge.
	already_scored = [
		{
			"skill": row.skill,
			"rating": row.rating,
			"is_not_applicable": bool(row.is_not_applicable),
			"comments": row.comments,
		}
		for row in (doc.ratings or [])
		if row.skill in compulsory_names
	]
	missing = [s for s in compulsory_names if s not in {r["skill"] for r in already_scored}]

	schedule = _find_schedule(doc)
	asked_skills: list[str] = []
	if schedule:
		# ? Asked questions per skill, for reference only -- a skill can still be rated from an
		# ? answer even when it was never asked as its own question, so this is a hint, not a
		# ? boundary on what counts as evidence. Rating on the schedule's own question rows is
		# ? the LiveKit call agent's business, not this agent's -- read-only context here.
		asked_skills = sorted(
			{
				row.skill
				for row in frappe.get_all(
					"Sanskar Interview Question",
					filters={"parent": schedule, "asked": 1},
					fields=["skill"],
				)
				if row.skill
			}
		)

	guards: list[str] = []
	if not designation:
		guards.append(
			"This review has no Appraisal Designation, so there are no compulsory skills to "
			"score. Report that and stop."
		)
	elif not compulsory_names:
		guards.append(
			f"Designation {designation} has no compulsory skills configured in PMS Skill Weight "
			"Item. Report that and stop -- there is nothing to score."
		)
	if doc.docstatus != 0:
		guards.append("This review is already submitted or cancelled. Report that and stop -- never rewrite it.")
	if not schedule:
		guards.append(
			"No Sanskar Interview Schedule is linked to this review's Interview, so there is no "
			"transcript to score from. Report that and stop."
		)

	return {
		"review": doc.name,
		"job_applicant": doc.job_applicant,
		"designation": designation,
		"docstatus": doc.docstatus,
		"schedule": schedule,
		"skills": skills,
		"already_scored": already_scored,
		"missing": missing,
		"asked_skills": asked_skills,
		"rating_scale": "1 = Needs Improvement, 2 = Developing, 3 = Proficient, 4 = Expert",
		"guards": guards,
		"how_to_write": (
			f"{len(skills)} compulsory skill(s) in total, {len(missing)} still missing. Score "
			"ONLY the skills named in `missing`. The ones in `already_scored` were rated during "
			"the call itself, by the agent that heard the answer as it was given -- that is "
			"better evidence than this transcript, so leave them exactly as they are and do not "
			"send them back. Call record_skill_scores as many times as you need -- send 5 to 8 "
			"skills per call rather than trying to fit all of them into one call. Each call "
			"tells you what is still `missing`; keep calling until that list is empty. Never end "
			"this run with only a text summary -- if record_skill_scores was never called, "
			"nothing was saved and HR sees no ratings at all."
		),
	}


# ==============================================================================
# TOOL 2 -- THE TRANSCRIPT
# ==============================================================================


def interview_skill_score_transcript(
	review: Annotated[str, "PMS Job Applicant Skill Review ID."],
	offset: Annotated[
		int,
		"Character offset to read from. Start at 0; when the result carries next_offset, call "
		"again with that value to read on.",
	] = 0,
	limit: Annotated[int | None, "Characters to return in this window. Defaults to the maximum."] = None,
) -> dict[str, Any]:
	"""Fetch the interview transcript for this review, from the Sanskar Interview Schedule its
	Interview is linked to, as plain `role: text` lines."""
	frappe.has_permission("PMS Job Applicant Skill Review", "read", review, throw=True)
	doc = frappe.get_doc("PMS Job Applicant Skill Review", review)

	schedule = _find_schedule(doc)
	if not schedule:
		return {
			"review": doc.name,
			"found": False,
			"transcript": None,
			"what_to_do": (
				"No Sanskar Interview Schedule is linked to this review's Interview. Report that "
				"and stop -- never guess a score without a transcript."
			),
		}

	frappe.has_permission("Sanskar Interview Schedule", "read", schedule, throw=True)
	transcript_json = frappe.db.get_value("Sanskar Interview Schedule", schedule, "transcript_json")
	turns = json.loads(transcript_json or "[]")
	if not turns:
		return {
			"review": doc.name,
			"schedule": schedule,
			"found": False,
			"transcript": None,
			"what_to_do": f"{schedule} has no transcript recorded yet. Report that and stop.",
		}

	full_text = "\n".join(f"{t.get('role') or 'user'}: {t.get('text') or ''}" for t in turns)

	window_limit = min(max(cint(limit) or DEFAULT_MAX_CHARS, 500), DEFAULT_MAX_CHARS)
	offset = max(cint(offset), 0)
	window = full_text[offset : offset + window_limit]

	result: dict[str, Any] = {
		"review": doc.name,
		"schedule": schedule,
		"found": True,
		"transcript": {
			"total_chars": len(full_text),
			"offset": offset,
			"chars": len(window),
			"turns": len(turns),
			"text": window,
		},
	}
	if offset + len(window) < len(full_text):
		result["transcript"]["next_offset"] = offset + len(window)
		result["transcript"]["note"] = (
			"This is one window of a longer transcript. Read the remaining windows before "
			"scoring anything -- a skill covered late in the call is still evidence for it."
		)
	return result


# ==============================================================================
# TOOL 3 -- WRITE THE SCORES
# ==============================================================================


def record_skill_scores(
	review: Annotated[str, "PMS Job Applicant Skill Review ID."],
	scores: Annotated[
		list[dict],
		"A BATCH of compulsory skills from interview_skill_score_context -- 5 to 8 per call is "
		"a good size, NOT all of them at once. Call this tool again with the next batch, and "
		"keep calling until the response's `missing` list is empty. Each entry: "
		'{"skill": "<exact skill name>", "rating": <integer 1-4, omit if not_applicable>, '
		'"not_applicable": <true when nothing in the transcript touched this skill>, '
		'"comments": "<a short quote from the transcript backing the rating, or explaining '
		'why nothing touched it>"}. '
		"A skill covered only in an answer -- never asked about directly -- still gets rated "
		"from that answer. Never guess a middling rating to fill a gap.",
	],
) -> dict[str, Any]:
	"""Write one batch of compulsory-skill ratings onto this review's own Skill Ratings table.
	Safe to call repeatedly with different skills -- each call merges its batch in and returns
	`missing`, the compulsory skills still left to score; call again with those until `missing`
	is empty. Refuses the whole batch if a skill in it is unknown, rated outside 1-4, has no
	comments, or carries both a rating and Not Applicable -- it never partially writes a bad
	batch. Any other row already on the review (a human's own extra skill) is left untouched.
	Never submits, and never touches the linked Sanskar Interview Schedule."""
	frappe.has_permission("PMS Job Applicant Skill Review", "write", review, throw=True)
	doc = frappe.get_doc("PMS Job Applicant Skill Review", review)

	if doc.docstatus != 0:
		frappe.throw(_("{0} is already submitted or cancelled. Nothing was written.").format(doc.name))

	from sanskar_erp.sanskar_pms.doctype.pms_job_applicant_skill_review.pms_job_applicant_skill_review import (
		get_score_weight_map,
	)

	designation = doc.appraisal_designation
	weight_map = get_score_weight_map(designation) if designation else {}
	compulsory = {skill for skill, meta in weight_map.items() if meta.get("is_compulsory")}
	if not compulsory:
		frappe.throw(_("{0} has no compulsory skills configured. Nothing to score.").format(designation))
	by_key = {_key(skill): skill for skill in compulsory}

	if not scores:
		frappe.throw(_("scores is empty. Send a batch of compulsory skills to score."))

	clean: dict[str, dict[str, Any]] = {}
	rejected: list[str] = []

	for entry in scores:
		if not isinstance(entry, dict):
			rejected.append(f"{entry!r} is not an object with skill/rating/comments")
			continue

		raw_name = str(entry.get("skill") or "").strip()
		# ? Matched on a punctuation/case-insensitive key, but always stored under the
		# ? CANONICAL PMS name -- never the model's own spelling of it. See `_key`.
		name = by_key.get(_key(raw_name))
		if not name:
			rejected.append(
				f"{raw_name or '(no skill)'} is not a compulsory skill for {designation}; "
				f"compulsory skills are {', '.join(sorted(compulsory))}"
			)
			continue
		if name in clean:
			rejected.append(f"{name} was scored twice")
			continue

		comments = str(entry.get("comments") or entry.get("evidence") or "").strip()
		if not comments:
			rejected.append(f"{name}: no evidence quoted from the transcript")
			continue

		not_applicable = bool(entry.get("not_applicable"))
		raw_rating = entry.get("rating")

		if not_applicable:
			if raw_rating not in (None, ""):
				rejected.append(f"{name}: marked not_applicable but also carries a rating")
				continue
			clean[name] = {"skill": name, "is_not_applicable": 1, "rating": None, "comments": comments}
			continue

		try:
			rating = round(float(raw_rating))
		except (TypeError, ValueError):
			rejected.append(f"{name}: rating {raw_rating!r} is not a number, and not_applicable was not set")
			continue
		if not 1 <= rating <= 4:
			rejected.append(f"{name}: rating {rating} is outside 1..4")
			continue
		no_evidence = _NO_EVIDENCE_PHRASES.search(comments)
		if no_evidence:
			rejected.append(
				f"{name}: rated {rating} but comments say {no_evidence.group(0)!r} -- that means "
				"the transcript never touched this skill, which is not_applicable, not a low "
				"rating. Resend this entry with not_applicable=true instead."
			)
			continue
		clean[name] = {"skill": name, "is_not_applicable": 0, "rating": rating, "comments": comments}

	if rejected:
		frappe.throw(_("These skill scores were refused, so nothing was written: {0}").format("; ".join(rejected)))

	# ? Merge, not replace: a row for a non-compulsory skill (a human's own extra rating) is
	# ? left exactly as it is. Only the compulsory rows THIS BATCH covers are touched -- a
	# ? compulsory skill from an earlier batch, or one still to come, is untouched by this call.
	by_skill = {row.skill: row for row in (doc.ratings or [])}
	for name, values in clean.items():
		row = by_skill.get(name)
		if row:
			row.rating = values["rating"]
			row.is_not_applicable = values["is_not_applicable"]
			row.comments = values["comments"]
		else:
			doc.append("ratings", values)

	doc.save(ignore_permissions=True)

	scored_so_far = {row.skill for row in doc.ratings if row.skill in compulsory}
	missing = sorted(compulsory - scored_so_far)

	return {
		"review": doc.name,
		"batch_rated": len([s for s in clean.values() if not s["is_not_applicable"]]),
		"batch_not_applicable": [s["skill"] for s in clean.values() if s["is_not_applicable"]],
		"missing": missing,
		"note": (
			"Written to this review's Skill Ratings table. Nothing was submitted, and the linked "
			"Sanskar Interview Schedule was not touched -- a human still reviews this before Mark "
			"Completed."
			if not missing
			else (
				f"Batch saved. {len(missing)} compulsory skill(s) still need scoring: "
				+ ", ".join(missing)
				+ ". Call record_skill_scores again with those -- the run is not done until "
				"`missing` comes back empty."
			)
		),
	}
