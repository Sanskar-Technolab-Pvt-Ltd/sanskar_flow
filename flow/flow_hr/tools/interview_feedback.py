# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Flow tools for the Interview Feedback Agent.

Registered as Flow Tool rows of type "Imported":
    interview_transcript       -> flow.flow_hr.tools.interview_feedback.interview_transcript
    interview_feedback_context -> flow.flow_hr.tools.interview_feedback.interview_feedback_context
    record_interview_feedback  -> flow.flow_hr.tools.interview_feedback.record_interview_feedback

The first two are read-only. The third writes, and is a tool rather than the built-in
`create` / `update` pair for one reason: `prompt_hr.py.interview_feedback.on_update`
computes the obtained score as `sum(custom_rating_given) / sum(custom_rating_scale) * 10`,
so a skill row saved without a rating scale is a ZeroDivisionError on save, and a rating
above the scale is refused by the form script. Handing a model the whole `skill_assessment`
table through `update` puts it one arithmetic slip away from either. `record_interview_feedback`
takes ratings by skill name, matches them onto the rows that already exist, and refuses the
save if anything is out of range.

WHERE THE TRANSCRIPT COMES FROM IS CONFIGURATION, NOT CODE.
Interviews here are recorded by whatever the panel happened to use -- a Teams export, a
note-taker bot, a vendor API, a recruiter pasting text onto the Interview. So no source is
hard-coded: `Interview Feedback Agent Settings.transcript_sources` is an ordered list of
places to look, each row a `Interview Transcript Source`, tried top to bottom until one
returns text. Five kinds of row are understood -- a field on the Interview, an attachment on
the Interview, an attachment on the Job Applicant, a Python method (the hook for a provider
integration that needs its own auth), and an HTTP endpoint. Whatever comes back is parsed
from WebVTT, SRT, JSON or plain text into one shape, so the agent reads the same thing
regardless of who recorded it. Adding a provider later is a new row, or at most a new
function pointed at by a `Python Method` row -- not an edit to this module.

Nothing here submits an Interview Feedback. The ratings are a draft for the interviewer to
confirm, and confirming them is a human's decision.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Annotated, Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_url, now_datetime

SETTINGS_DOCTYPE = "Interview Feedback Agent Settings"

# ? EXTENSIONS THIS MODULE DECODES ITSELF. flow's extractor rejects vtt/srt outright
# ? (they are not in its TEXT_EXTENSIONS), and they are the two commonest exports from
# ? a meeting recorder, so they have to be handled here.
PLAIN_EXTENSIONS = {
	"txt",
	"text",
	"md",
	"markdown",
	"log",
	"vtt",
	"srt",
	"json",
	"csv",
	"tsv",
}

# ? DEFAULTS, USED ONLY WHEN THE SETTINGS SINGLE HAS NEVER BEEN SAVED.
DEFAULT_MIN_CHARS = 400
DEFAULT_MAX_CHARS = 60000
DEFAULT_RATING_SCALE = 10

MAX_ATTEMPT_DETAIL = 300
URL_TIMEOUT = 20
MAX_FETCH_BYTES = 10 * 1024 * 1024

# ? SPEAKER ROLES THE AGENT IS ALLOWED TO SEE. Anything unrecognised stays None rather
# ? than being guessed at -- attributing the candidate's answer to the interviewer would
# ? invert the whole assessment.
ROLE_INTERVIEWER = "Interviewer"
ROLE_CANDIDATE = "Candidate"

TIMESTAMP_LINE = re.compile(r"^\s*[\[(<]?(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)[\])>]?\s*[-–]?\s*(.*)$")  # noqa: RUF001
SPEAKER_PREFIX = re.compile(r"^\s*([A-Za-z0-9 .,'&/_-]{1,60}?)\s*:\s*(.*)$")
VTT_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", re.IGNORECASE | re.DOTALL)
VTT_CUE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})")
TAG = re.compile(r"<[^>]+>")


# ==============================================================================
# TOOL 1 -- THE TRANSCRIPT
# ==============================================================================


def interview_transcript(
	interview: Annotated[str, "Interview ID, e.g. HR-INT-2026-0001. Never the candidate's name or email."],
	source: Annotated[
		str | None,
		"Optional label of one configured transcript source to use instead of the whole "
		"ordered list, e.g. 'Transcript text on Interview'. Leave empty on the first call.",
	] = None,
	offset: Annotated[
		int,
		"Character offset to read from. Start at 0; when the result carries next_offset, "
		"call again with that value to read on.",
	] = 0,
	limit: Annotated[
		int | None,
		"Characters to return in this window. Defaults to the configured maximum.",
	] = None,
) -> dict[str, Any]:
	"""Fetch and normalise the interview transcript from whichever configured source has it."""
	frappe.has_permission("Interview", "read", interview, throw=True)
	settings = _settings()
	doc = frappe.get_doc("Interview", interview)

	rows = _source_rows(settings, source)
	resolved, attempts = _resolve_transcript(doc, rows)

	result: dict[str, Any] = {
		"interview": doc.name,
		"job_applicant": doc.job_applicant,
		"interview_type": doc.interview_type,
		"scheduled_on": str(doc.scheduled_on or ""),
		"attempts": attempts,
		"found": bool(resolved),
	}

	if not resolved:
		result["transcript"] = None
		result["what_to_do"] = (
			"No transcript is available for this interview. Say so, name the sources that were "
			"tried, and stop -- do not rate anything. Whoever recorded the interview has to file "
			"the transcript first (or a source for their tool has to be added to "
			f"{SETTINGS_DOCTYPE})."
		)
		if not rows:
			result["what_to_do"] = (
				f"No transcript sources are configured at all. Ask an administrator to add at "
				f"least one row to {SETTINGS_DOCTYPE} -> Sources, then run again."
			)
		return result

	speakers = _label_speakers(doc, resolved["segments"], resolved.get("speaker_map") or {})
	lines = [_render_segment(seg) for seg in resolved["segments"]]
	full_text = "\n".join(lines)

	max_chars = cint(settings.get("max_transcript_chars")) or DEFAULT_MAX_CHARS
	min_chars = cint(settings.get("min_transcript_chars")) or DEFAULT_MIN_CHARS
	window_limit = min(max(cint(limit) or max_chars, 500), max_chars)
	offset = max(cint(offset), 0)
	window = full_text[offset : offset + window_limit]

	transcript: dict[str, Any] = {
		"source": resolved["source"],
		"format": resolved["format"],
		"total_chars": len(full_text),
		"offset": offset,
		"chars": len(window),
		"turns": len(resolved["segments"]),
		"speakers": speakers,
		"text": window,
	}
	if offset + len(window) < len(full_text):
		transcript["next_offset"] = offset + len(window)
		transcript["note"] = (
			"This is one window of a longer transcript. Read the remaining windows before rating "
			"anything -- the second half of an interview is usually where the depth is."
		)

	warnings = list(resolved.get("warnings") or [])
	if len(full_text) < min_chars:
		warnings.append(
			f"The transcript is only {len(full_text)} characters, below the configured minimum of "
			f"{min_chars}. Treat it as unusable: report it and rate nothing."
		)
		transcript["usable"] = False
	else:
		transcript["usable"] = True

	if not any(s["role"] == ROLE_CANDIDATE for s in speakers):
		warnings.append(
			"No speaker could be matched to the candidate. Do not assume which side is which -- "
			"if the roles are not obvious from the content itself, report that and rate nothing."
		)
	if not any(s["role"] == ROLE_INTERVIEWER for s in speakers):
		warnings.append("No speaker could be matched to an interviewer on this Interview.")

	transcript["warnings"] = warnings
	result["transcript"] = transcript
	return result


# ==============================================================================
# TOOL 2 -- THE RUBRIC AND EVERYTHING ELSE AROUND THE INTERVIEW
# ==============================================================================


def interview_feedback_context(
	interview: Annotated[str, "Interview ID, e.g. HR-INT-2026-0001. Never the candidate's name or email."],
) -> dict[str, Any]:
	"""Everything needed to judge one interview: the rubric, the candidate, the panel, and the feedback records already waiting."""
	frappe.has_permission("Interview", "read", interview, throw=True)
	settings = _settings()
	doc = frappe.get_doc("Interview", interview)

	rubric = _rubric(doc.interview_type, settings)
	applicant = _applicant(doc.job_applicant)
	panel = _panel(doc)
	feedback = _feedback_records(doc, rubric)

	guards: list[str] = []
	if not rubric["skills"]:
		guards.append(
			f"Interview Type {doc.interview_type} has no Expected Skill Set, so there is nothing "
			"to rate. Report that and stop -- never invent a skill."
		)
	if doc.docstatus == 2:
		guards.append("This Interview is cancelled. Write nothing.")
	if not feedback["writable"] and settings.get("write_mode") == "Fill Interviewer Draft":
		guards.append(
			"No draft Interview Feedback is open for this interview, so there is nothing to fill "
			"in. record_interview_feedback will fall back to a comment on the Interview."
		)

	return {
		"interview": {
			"name": doc.name,
			"interview_type": doc.interview_type,
			"status": doc.status,
			"docstatus": doc.docstatus,
			"scheduled_on": str(doc.scheduled_on or ""),
			"from_time": str(doc.from_time or ""),
			"to_time": str(doc.to_time or ""),
			"designation": doc.designation,
			"job_opening": doc.job_opening,
			"department": doc.get("custom_department"),
			"company": doc.get("custom_company"),
			"mode": doc.get("custom_mode"),
			"expected_average_rating": flt(doc.expected_average_rating),
			"interview_summary": doc.interview_summary,
			"transcript_source_stamp": doc.get("custom_transcript_source"),
			"transcript_received_on": str(doc.get("custom_transcript_received_on") or ""),
		},
		"applicant": applicant,
		"rubric": rubric,
		"panel": panel,
		"feedback_records": feedback["records"],
		"write_mode": settings.get("write_mode") or "Suggest Only",
		"require_evidence": bool(cint(settings.get("require_evidence"))),
		"unrated_skill_policy": settings.get("unrated_skill_policy") or "Escalate",
		"overwrite_existing_ratings": bool(cint(settings.get("overwrite_existing_ratings"))),
		"guards": guards,
	}


# ==============================================================================
# TOOL 3 -- WRITE THE ASSESSMENT
# ==============================================================================


def record_interview_feedback(
	interview: Annotated[str, "Interview ID, e.g. HR-INT-2026-0001."],
	summary: Annotated[
		str,
		"The written assessment: what the candidate demonstrated, where they fell short, and "
		"the recommendation. Plain prose, no markdown headings. Every claim must be traceable "
		"to the transcript.",
	],
	ratings: Annotated[
		list[dict],
		"One entry per rubric skill you are rating: "
		'{"skill": "<exact skill name from the rubric>", "rating_given": <integer, 0..rating_scale>, '
		'"evidence": "<a short quote from the transcript that justifies it>"}. '
		"Omit a skill the transcript never touched -- do not guess a middling score for it.",
	],
	unrated: Annotated[
		list[str] | None,
		"Rubric skills the transcript gave you nothing on. Named for the interviewer to cover.",
	] = None,
	recommendation: Annotated[
		str | None,
		"Your read of the outcome: 'Cleared', 'Rejected' or 'Inconclusive'. Recorded as advice "
		"only -- it never sets the result on the record.",
	] = None,
	interviewer: Annotated[
		str | None,
		"Restrict the write to one interviewer's draft feedback, by their user email. Empty "
		"writes to every open draft on this interview.",
	] = None,
) -> dict[str, Any]:
	"""Record the assessment against this interview. Fills the interviewers' draft feedback or comments on the Interview, per the configured write mode. Never submits."""
	frappe.has_permission("Interview", "read", interview, throw=True)
	settings = _settings()
	if not cint(settings.get("enabled")):
		frappe.throw(_("{0} is disabled. Nothing was written.").format(SETTINGS_DOCTYPE))

	doc = frappe.get_doc("Interview", interview)
	if doc.docstatus == 2:
		frappe.throw(_("Interview {0} is cancelled. Nothing was written.").format(doc.name))

	summary = (summary or "").strip()
	if not summary:
		frappe.throw(_("summary is empty. Write the assessment before recording it."))

	rubric = _rubric(doc.interview_type, settings)
	unrated = [u for u in (unrated or []) if u]
	# ? A ROUND THAT COULD NOT BE ASSESSED IS A RESULT, AND IT HAS TO BE VISIBLE. If the
	# ? roles could not be established, or nothing on the rubric came up, the agent must
	# ? still be able to record that -- otherwise the only trace is a Flow Run nobody opens.
	# ? Allowed only when `unrated` says what was not assessed, so it can never be the shape
	# ? of an accidentally empty write.
	clean, rejected = _validate_ratings(ratings, rubric, settings, allow_empty=bool(unrated))
	if rejected:
		frappe.throw(
			_("These ratings were refused, so nothing was written: {0}").format("; ".join(rejected))
		)
	body = _feedback_body(settings, summary, clean, unrated, recommendation)

	write_mode = settings.get("write_mode") or "Suggest Only"
	written: dict[str, Any] = {
		"interview": doc.name,
		"write_mode": write_mode,
		"rated_skills": [r["skill"] for r in clean],
		"unrated_skills": unrated,
		"recommendation": recommendation,
		"submitted": False,
	}

	if write_mode == "Fill Interviewer Draft":
		filled, skipped = _fill_drafts(doc, clean, body, interviewer, settings)
		written["feedback_filled"] = filled
		written["feedback_skipped"] = skipped
		if not filled:
			written["comment"] = _comment_on_interview(doc, body)
			written["fell_back_to_comment"] = True
	else:
		written["comment"] = _comment_on_interview(doc, body)

	written["note"] = (
		"Nothing was submitted. The interviewer still has to check these ratings against the "
		"recording and submit the Interview Feedback themselves."
	)
	return written


# ==============================================================================
# SETTINGS
# ==============================================================================


def _settings() -> dict[str, Any]:
	"""The settings single as a plain dict, with the child table intact. Returns the
	documented defaults when the single has never been saved, so a fresh bench does not
	fail on a missing row."""
	try:
		doc = frappe.get_cached_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
	except frappe.DoesNotExistError:
		return {
			"enabled": 1,
			"min_transcript_chars": DEFAULT_MIN_CHARS,
			"max_transcript_chars": DEFAULT_MAX_CHARS,
			"default_rating_scale": DEFAULT_RATING_SCALE,
			"require_evidence": 1,
			"unrated_skill_policy": "Escalate",
			"write_mode": "Suggest Only",
			"overwrite_existing_ratings": 0,
			"feedback_note": "AI-drafted from the interview transcript.",
			"transcript_sources": [],
		}
	return doc.as_dict()


def _source_rows(settings: dict[str, Any], only: str | None) -> list[dict[str, Any]]:
	rows = [r for r in (settings.get("transcript_sources") or []) if cint(r.get("enabled"))]
	if only:
		wanted = only.strip().lower()
		rows = [r for r in rows if (r.get("source_label") or "").strip().lower() == wanted]
	return rows


# ==============================================================================
# SOURCE RESOLUTION -- ONE ROW AT A TIME, FIRST HIT WINS
# ==============================================================================


def _resolve_transcript(doc, rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
	attempts: list[dict[str, Any]] = []

	for row in rows:
		label = row.get("source_label") or row.get("source_type")
		try:
			raw, hint, origin = _fetch_source(doc, row)
		except Exception as e:
			attempts.append(
				{
					"source": label,
					"type": row.get("source_type"),
					"status": "error",
					"detail": _short(str(e)),
				}
			)
			continue

		if not raw:
			attempts.append(
				{"source": label, "type": row.get("source_type"), "status": "empty", "detail": origin}
			)
			continue

		fmt = row.get("transcript_format") or "Auto"
		segments, resolved_format = _parse(raw, fmt if fmt != "Auto" else _sniff(raw, hint))
		segments = [s for s in segments if (s.get("text") or "").strip()]

		if not segments:
			attempts.append(
				{
					"source": label,
					"type": row.get("source_type"),
					"status": "unparsed",
					"detail": f"{origin} -- read as {resolved_format} but no dialogue came out of it",
				}
			)
			continue

		attempts.append(
			{
				"source": label,
				"type": row.get("source_type"),
				"status": "used",
				"detail": f"{origin} -- {len(segments)} turns, read as {resolved_format}",
			}
		)
		return (
			{
				"source": label,
				"origin": origin,
				"format": resolved_format,
				"segments": segments,
				"speaker_map": _json_or_empty(row.get("speaker_map")),
				"warnings": [],
			},
			attempts,
		)

	return None, attempts


def _fetch_source(doc, row: dict[str, Any]) -> tuple[Any, str, str]:
	"""Return (raw payload, format hint, human description of where it came from)."""
	source_type = row.get("source_type")

	if source_type == "Field on Interview":
		return _fetch_field(doc, row)
	if source_type == "Attachment on Interview":
		return _fetch_attachment("Interview", doc.name, row)
	if source_type == "Attachment on Job Applicant":
		if not doc.job_applicant:
			return None, "", "the Interview has no Job Applicant"
		return _fetch_attachment("Job Applicant", doc.job_applicant, row)
	if source_type == "Python Method":
		return _fetch_method(doc, row)
	if source_type == "HTTP Endpoint":
		return _fetch_endpoint(doc, row)

	raise ValueError(f"Unknown transcript source type {source_type!r}")


def _fetch_field(doc, row: dict[str, Any]) -> tuple[Any, str, str]:
	fieldname = (row.get("fieldname") or "").strip()
	meta = frappe.get_meta("Interview")
	df = meta.get_field(fieldname)
	if not df:
		raise ValueError(f"Interview has no field {fieldname!r}. Fix the source row.")

	value = doc.get(fieldname)
	if not value:
		return None, "", f"{fieldname} is empty"

	if df.fieldtype in ("Attach", "Attach Image"):
		text, name = _file_text(value)
		return text, os.path.splitext(name or value)[1].lstrip("."), f"{fieldname} -> {name or value}"

	return value, "", f"{fieldname} on {doc.name}"


def _fetch_attachment(doctype: str, name: str, row: dict[str, Any]) -> tuple[Any, str, str]:
	pattern = (row.get("file_name_pattern") or "").strip()
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name},
		fields=["name", "file_name", "file_url", "creation"],
		order_by="creation desc",
	)
	if pattern:
		files = [f for f in files if fnmatch.fnmatch((f.file_name or "").lower(), pattern.lower())]
	if not files:
		where = f"{doctype} {name}"
		return None, "", f"nothing attached to {where}" + (f" matching {pattern}" if pattern else "")

	last_error = ""
	for f in files:
		try:
			text, _name = _file_text(f.file_url or f.name)
		except Exception as e:
			last_error = _short(str(e))
			continue
		if text:
			ext = os.path.splitext(f.file_name or f.file_url or "")[1].lstrip(".")
			return text, ext, f"{f.file_name} attached to {doctype} {name}"

	return None, "", f"no readable attachment on {doctype} {name}" + (f" ({last_error})" if last_error else "")


def _fetch_method(doc, row: dict[str, Any]) -> tuple[Any, str, str]:
	path = (row.get("method_path") or "").strip()
	fn = frappe.get_attr(path)
	# ? A dotted path out of a settings table is as powerful as a Server Script, so hold it
	# ? to the same bar as an API call: it has to be whitelisted on purpose.
	frappe.is_whitelisted(fn)
	payload = fn(interview=doc.name)

	if isinstance(payload, dict):
		if payload.get("file_url"):
			text, name = _file_text(payload["file_url"])
			return text, os.path.splitext(name or "")[1].lstrip("."), f"{path} -> {name}"
		payload = _json_path(payload, row.get("json_path")) if row.get("json_path") else payload
	return payload, "", f"{path}"


def _fetch_endpoint(doc, row: dict[str, Any]) -> tuple[Any, str, str]:
	import requests

	url = frappe.render_template(row.get("endpoint") or "", {"doc": doc, "now": now_datetime()}).strip()
	_validate_public_url(url)

	headers = _render_json(row.get("headers"), doc)
	payload = _render_json(row.get("payload"), doc)
	method = (row.get("request_method") or "GET").upper()

	response = requests.request(
		method,
		url,
		headers={"User-Agent": "prompt-hr-interview-feedback/1.0", **(headers or {})},
		json=payload or None,
		timeout=URL_TIMEOUT,
		stream=True,
	)
	response.raise_for_status()
	raw = response.raw.read(MAX_FETCH_BYTES + 1, decode_content=True) or b""
	if len(raw) > MAX_FETCH_BYTES:
		raise ValueError(f"{url} returned more than {MAX_FETCH_BYTES} bytes.")

	body = raw.decode("utf-8", errors="replace")
	content_type = (response.headers.get("Content-Type") or "").lower()

	if "json" in content_type or body.lstrip()[:1] in ("{", "["):
		try:
			parsed = json.loads(body)
		except ValueError:
			return body, "", url
		return _json_path(parsed, row.get("json_path")), "json", url

	return body, os.path.splitext(url.split("?")[0])[1].lstrip("."), url


def _file_text(reference: str) -> tuple[str, str]:
	"""Text of an attachment, by File name or file_url. Decodes the plain formats itself --
	including vtt and srt, which flow's extractor refuses -- and hands anything else
	(pdf, docx, xlsx, a scan) to flow's extractor, which OCRs when it has to."""
	file_doc = None
	if frappe.db.exists("File", reference):
		file_doc = frappe.get_doc("File", reference)
	else:
		site = (get_url() or "").rstrip("/")
		if site and reference.startswith(site + "/"):
			reference = reference[len(site) :]
		name = frappe.db.get_value("File", {"file_url": reference}, "name")
		if not name:
			raise ValueError(f"No File record for {reference!r}.")
		file_doc = frappe.get_doc("File", name)

	if file_doc.attached_to_doctype and file_doc.attached_to_name:
		frappe.has_permission(
			file_doc.attached_to_doctype, "read", file_doc.attached_to_name, throw=True
		)

	extension = os.path.splitext(file_doc.file_name or file_doc.file_url or "")[1].lower().lstrip(".")
	if extension in PLAIN_EXTENSIONS:
		content = file_doc.get_content()
		if isinstance(content, bytes):
			content = content.decode("utf-8", errors="replace")
		return content, file_doc.file_name or file_doc.file_url

	from flow.knowledge.extract import extract_file

	return extract_file(file_doc), file_doc.file_name or file_doc.file_url


def _validate_public_url(url: str) -> None:
	"""Refuse anything that is not a public http(s) address. A transcript endpoint is
	configured by a System Manager, but it still must not be usable to make the server
	fetch its own metadata service or an internal admin page."""
	import ipaddress
	import socket
	from urllib.parse import urlparse

	parsed = urlparse(url)
	if parsed.scheme not in ("http", "https"):
		raise ValueError(f"Transcript endpoint must be http or https, got {url!r}.")
	if not parsed.hostname:
		raise ValueError(f"Transcript endpoint has no host: {url!r}.")

	try:
		infos = socket.getaddrinfo(parsed.hostname, None)
	except OSError as e:
		raise ValueError(f"Cannot resolve {parsed.hostname}: {e}") from e

	for info in infos:
		address = ipaddress.ip_address(info[4][0])
		if (
			address.is_private
			or address.is_loopback
			or address.is_link_local
			or address.is_reserved
			or address.is_multicast
		):
			raise ValueError(f"{parsed.hostname} resolves to the non-public address {address}.")


def _render_json(template: str | None, doc) -> dict[str, Any] | None:
	raw = (template or "").strip()
	if not raw:
		return None
	rendered = frappe.render_template(raw, {"doc": doc, "now": now_datetime()})
	try:
		value = json.loads(rendered)
	except ValueError as e:
		raise ValueError(f"Rendered JSON is invalid: {e}") from e
	return value if isinstance(value, dict) else None


def _json_or_empty(raw: str | None) -> dict[str, Any]:
	try:
		value = json.loads((raw or "").strip() or "{}")
	except ValueError:
		return {}
	return value if isinstance(value, dict) else {}


def _json_path(payload: Any, path: str | None) -> Any:
	"""Walk a dotted path into a decoded JSON payload. A missing key returns None rather
	than raising, so a provider that changed its shape reads as 'empty source' and the next
	configured source gets its turn."""
	if not path:
		return payload
	current = payload
	for part in str(path).split("."):
		if part == "":
			continue
		if isinstance(current, list):
			if not part.isdigit() or int(part) >= len(current):
				return None
			current = current[int(part)]
		elif isinstance(current, dict):
			if part not in current:
				return None
			current = current[part]
		else:
			return None
	return current


# ==============================================================================
# PARSING -- EVERY PROVIDER'S SHAPE INTO ONE LIST OF TURNS
# ==============================================================================


def _sniff(raw: Any, extension_hint: str) -> str:
	if isinstance(raw, (dict, list)):
		return "JSON"

	text = str(raw or "")
	hint = (extension_hint or "").lower()
	if hint == "vtt":
		return "WebVTT"
	if hint == "srt":
		return "SRT"
	if hint == "json":
		return "JSON"

	head = text.lstrip()[:2000]
	if head.startswith("WEBVTT"):
		return "WebVTT"
	# ? A LEADING BRACKET IS NOT ENOUGH. `[00:04:12] Sahil: ...` is the commonest plain
	# ? transcript there is, and it starts with the same character a JSON array does -- so
	# ? the text has to actually decode before it is treated as JSON.
	if head[:1] in ("{", "[") and _is_json(text):
		return "JSON"
	if re.search(r"^\s*\d+\s*$", head, re.MULTILINE) and "-->" in head:
		return "SRT"
	if "-->" in head:
		return "WebVTT"
	return "Plain Text"


def _is_json(text: str) -> bool:
	try:
		json.loads(text)
	except ValueError:
		return False
	return True


def _parse(raw: Any, fmt: str) -> tuple[list[dict[str, Any]], str]:
	if fmt == "JSON":
		segments = _parse_json(raw)
		if segments:
			return segments, "JSON"
		# ? A JSON payload that carries its transcript as one text blob -- common for
		# ? providers that hand back {"transcript": "..."} -- still has to be read. The
		# ? fallback is never allowed to be JSON again: that is how this recursed forever.
		fallback = _json_text_blob(raw)
		if fallback:
			inner = _sniff(fallback, "")
			return _parse(fallback, "Plain Text" if inner == "JSON" else inner)
		return [], "JSON"

	text = str(raw or "")
	if fmt == "WebVTT":
		return _parse_vtt(text), "WebVTT"
	if fmt == "SRT":
		return _parse_srt(text), "SRT"
	return _parse_plain(text), "Plain Text"


def _parse_json(payload: Any) -> list[dict[str, Any]]:
	"""Pull turns out of the JSON shapes meeting tools actually emit. Looks for a list of
	turn-like objects, wherever it sits in the payload."""
	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except ValueError:
			return []

	rows = _find_turn_list(payload)
	segments: list[dict[str, Any]] = []
	for row in rows or []:
		if not isinstance(row, dict):
			if isinstance(row, str) and row.strip():
				segments.append({"time": "", "speaker": "", "text": row.strip()})
			continue
		text = _first(row, ("text", "content", "sentence", "utterance", "value", "caption", "body"))
		if not text:
			words = row.get("words")
			if isinstance(words, list):
				text = " ".join(
					str(_first(w, ("word", "text")) or "") for w in words if isinstance(w, dict)
				).strip()
		if not text:
			continue
		segments.append(
			{
				"time": _stamp(_first(row, ("start", "start_time", "startTime", "offset", "timestamp", "time"))),
				"speaker": str(
					_first(row, ("speaker", "speaker_name", "speakerName", "displayName", "name", "role", "participant"))
					or ""
				).strip(),
				"text": TAG.sub("", str(text)).strip(),
			}
		)
	return segments


def _find_turn_list(payload: Any, depth: int = 0) -> list | None:
	"""Depth-first hunt for the list of turns. Prefers the conventional keys, then settles
	for any list of dicts that looks like dialogue."""
	if depth > 6:
		return None
	if isinstance(payload, list):
		return payload if _looks_like_turns(payload) else None
	if not isinstance(payload, dict):
		return None

	for key in (
		"segments",
		"transcript",
		"transcripts",
		"entries",
		"utterances",
		"sentences",
		"monologues",
		"results",
		"items",
		"messages",
		"turns",
		"captions",
		"data",
	):
		if key in payload:
			found = _find_turn_list(payload[key], depth + 1)
			if found:
				return found

	for value in payload.values():
		found = _find_turn_list(value, depth + 1)
		if found:
			return found
	return None


def _looks_like_turns(rows: list) -> bool:
	dicts = [r for r in rows if isinstance(r, dict)]
	if not dicts:
		return bool(rows) and all(isinstance(r, str) for r in rows)
	keys = {k.lower() for r in dicts[:5] for k in r}
	return bool(keys & {"text", "content", "sentence", "utterance", "caption", "words", "body"})


def _json_text_blob(payload: Any) -> str:
	if isinstance(payload, str):
		return payload
	if not isinstance(payload, dict):
		return ""
	for key in ("transcript", "text", "content", "body", "transcription", "raw"):
		value = payload.get(key)
		if isinstance(value, str) and value.strip():
			return value
	return ""


def _parse_vtt(text: str) -> list[dict[str, Any]]:
	segments: list[dict[str, Any]] = []
	for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
		lines = [ln for ln in block.split("\n") if ln.strip()]
		if not lines or lines[0].strip().upper().startswith("WEBVTT"):
			continue

		stamp = ""
		body_lines: list[str] = []
		for line in lines:
			cue = VTT_CUE.search(line)
			if cue and not body_lines:
				stamp = _trim_stamp(cue.group(1))
				continue
			if not stamp and re.match(r"^[\w-]+$", line.strip()) and not body_lines:
				continue  # ? cue identifier
			body_lines.append(line)

		body = " ".join(body_lines).strip()
		if not body:
			continue

		speaker = ""
		voice = VTT_VOICE.search(body)
		if voice:
			speaker = voice.group(1).strip()
			body = voice.group(2)
		body = TAG.sub("", body).strip()
		if not speaker:
			speaker, body = _split_speaker(body)
		if body:
			segments.append({"time": stamp, "speaker": speaker, "text": body})
	return _merge_runs(segments)


def _parse_srt(text: str) -> list[dict[str, Any]]:
	segments: list[dict[str, Any]] = []
	for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
		lines = [ln for ln in block.split("\n") if ln.strip()]
		if not lines:
			continue
		if lines[0].strip().isdigit():
			lines = lines[1:]
		stamp = ""
		if lines and "-->" in lines[0]:
			cue = VTT_CUE.search(lines[0])
			stamp = _trim_stamp(cue.group(1)) if cue else ""
			lines = lines[1:]
		body = TAG.sub("", " ".join(lines)).strip()
		if not body:
			continue
		speaker, body = _split_speaker(body)
		if body:
			segments.append({"time": stamp, "speaker": speaker, "text": body})
	return _merge_runs(segments)


def _parse_plain(text: str) -> list[dict[str, Any]]:
	"""Plain transcripts come in two habits: `[00:04:12] Name: text`, and a blank-line-
	separated wall of `Name: text`. Both end up as the same turns."""
	segments: list[dict[str, Any]] = []
	current: dict[str, Any] | None = None

	for raw_line in text.replace("\r\n", "\n").split("\n"):
		line = raw_line.strip()
		if not line:
			continue
		if "-->" in line or line.upper().startswith("WEBVTT") or line.isdigit():
			continue

		stamp = ""
		stamped = TIMESTAMP_LINE.match(line)
		if stamped and stamped.group(2):
			stamp = _trim_stamp(stamped.group(1))
			line = stamped.group(2).strip()

		speaker, body = _split_speaker(line)
		if speaker:
			if current:
				segments.append(current)
			current = {"time": stamp, "speaker": speaker, "text": body}
		elif current:
			current["text"] = f"{current['text']} {line}".strip()
		else:
			current = {"time": stamp, "speaker": "", "text": line}

	if current:
		segments.append(current)
	return _merge_runs([s for s in segments if (s.get("text") or "").strip()])


def _split_speaker(line: str) -> tuple[str, str]:
	"""`Name: what they said` -> ("Name", "what they said"). Refuses to treat a sentence
	that merely contains a colon as a speaker label."""
	match = SPEAKER_PREFIX.match(line)
	if not match:
		return "", line
	candidate, rest = match.group(1).strip(), match.group(2).strip()
	if not candidate or len(candidate.split()) > 5 or candidate.endswith((".", "?", "!")):
		return "", line
	return candidate, rest


def _merge_runs(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Caption formats cut a single sentence across cues. Glue consecutive cues from the
	same speaker back into one turn so the agent reads sentences, not fragments."""
	merged: list[dict[str, Any]] = []
	for seg in segments:
		if merged and merged[-1]["speaker"] == seg["speaker"] and seg["speaker"] != "":
			merged[-1]["text"] = f"{merged[-1]['text']} {seg['text']}".strip()
			continue
		if merged and not seg["speaker"] and not merged[-1]["speaker"]:
			merged[-1]["text"] = f"{merged[-1]['text']} {seg['text']}".strip()
			continue
		merged.append(dict(seg))
	return merged


def _stamp(value: Any) -> str:
	"""Provider start times arrive as seconds, milliseconds, or an already-formatted
	timestamp. Normalise to mm:ss / h:mm:ss."""
	if value in (None, ""):
		return ""
	if isinstance(value, str) and ":" in value:
		return _trim_stamp(value)
	try:
		seconds = float(value)
	except (TypeError, ValueError):
		return ""
	if seconds > 100000:  # ? almost certainly milliseconds
		seconds /= 1000.0
	seconds = int(seconds)
	hours, remainder = divmod(seconds, 3600)
	minutes, secs = divmod(remainder, 60)
	return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _trim_stamp(stamp: str) -> str:
	stamp = (stamp or "").strip().replace(",", ".").split(".")[0]
	if stamp.startswith("00:") and stamp.count(":") == 2:
		stamp = stamp[3:]
	return stamp


def _first(row: dict, keys: tuple[str, ...]) -> Any:
	for key in keys:
		if key in row and row[key] not in (None, ""):
			return row[key]
		for actual in row:
			if actual.lower() == key.lower() and row[actual] not in (None, ""):
				return row[actual]
	return None


# ==============================================================================
# SPEAKERS -> ROLES
# ==============================================================================


def _label_speakers(doc, segments: list[dict[str, Any]], speaker_map: dict[str, Any]) -> list[dict[str, Any]]:
	"""Decide, per speaker label, whether that voice is an interviewer or the candidate.
	Matched against this Interview's own panel and applicant, then against the configured
	map. A label that matches neither keeps role None -- guessing would risk crediting the
	interviewer's own explanation to the candidate."""
	panel = _panel(doc)
	panel_names = {_key(row.get("name")) for row in panel if row.get("name")}
	panel_names |= {_key(row.get("user")) for row in panel if row.get("user")}
	panel_names.discard("")

	applicant = _applicant(doc.job_applicant)
	candidate_keys = {
		_key(applicant.get("applicant_name")),
		_key(applicant.get("email")),
		_key(doc.get("custom_applicant_name")),
	}
	candidate_keys.discard("")

	configured = {_key(k): str(v) for k, v in (speaker_map or {}).items()}

	counts: dict[str, dict[str, Any]] = {}
	for seg in segments:
		label = (seg.get("speaker") or "").strip() or "(unlabelled)"
		bucket = counts.setdefault(label, {"label": label, "turns": 0, "words": 0})
		bucket["turns"] += 1
		bucket["words"] += len((seg.get("text") or "").split())

	speakers: list[dict[str, Any]] = []
	for label, bucket in counts.items():
		key = _key(label)
		role = configured.get(key)
		if role not in (ROLE_INTERVIEWER, ROLE_CANDIDATE):
			role = None
		if role is None:
			if key in candidate_keys or any(key and key in c for c in candidate_keys if c):
				role = ROLE_CANDIDATE
			elif key in panel_names or any(key and key in p for p in panel_names if p):
				role = ROLE_INTERVIEWER
		bucket["role"] = role
		speakers.append(bucket)

	# ? A two-voice transcript where exactly one side is identified: the other side is the
	# ? one that is left. Safe, and it rescues the common "Speaker 1 / Speaker 2" export.
	unknown = [s for s in speakers if not s["role"]]
	known_roles = {s["role"] for s in speakers if s["role"]}
	if len(speakers) == 2 and len(unknown) == 1 and len(known_roles) == 1:
		unknown[0]["role"] = ROLE_CANDIDATE if ROLE_INTERVIEWER in known_roles else ROLE_INTERVIEWER

	for seg in segments:
		label = (seg.get("speaker") or "").strip() or "(unlabelled)"
		seg["role"] = next((s["role"] for s in speakers if s["label"] == label), None)

	return sorted(speakers, key=lambda s: -s["words"])


def _key(value: Any) -> str:
	return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _render_segment(seg: dict[str, Any]) -> str:
	who = seg.get("speaker") or "?"
	role = seg.get("role")
	if role:
		who = f"{who} [{role}]"
	stamp = f"[{seg['time']}] " if seg.get("time") else ""
	return f"{stamp}{who}: {seg.get('text') or ''}".strip()


# ==============================================================================
# THE RUBRIC, THE CANDIDATE, THE PANEL
# ==============================================================================


def _rubric(interview_type: str | None, settings: dict[str, Any]) -> dict[str, Any]:
	"""The skills this round is scored on, straight off the Interview Type. This is the
	whole rubric: the Interview Feedback form does not allow rows to be added, so a skill
	that is not here cannot be rated, and inventing one would fail the Skill link anyway."""
	default_scale = cint(settings.get("default_rating_scale")) or DEFAULT_RATING_SCALE
	skills: list[dict[str, Any]] = []

	if interview_type:
		rows = frappe.get_all(
			"Expected Skill Set",
			filters={"parent": interview_type, "parenttype": "Interview Type"},
			fields=["skill", "custom_skill_type", "custom_rating_scale", "description"],
			order_by="idx",
		)
		seen: set[str] = set()
		for row in rows:
			if not row.skill or row.skill in seen:
				continue
			seen.add(row.skill)
			skills.append(
				{
					"skill": row.skill,
					"skill_type": row.custom_skill_type,
					"rating_scale": cint(row.custom_rating_scale) or default_scale,
					"scale_from_default": not cint(row.custom_rating_scale),
					"what_to_look_for": row.description,
				}
			)

	expected = flt(frappe.db.get_value("Interview Type", interview_type, "expected_average_rating")) if interview_type else 0.0
	return {
		"interview_type": interview_type,
		"skills": skills,
		"expected_average_rating": expected,
		"how_to_rate": (
			"Rate each skill as a whole number from 0 to its rating_scale, and quote the "
			"transcript line that justifies it. A skill the interview never covered is not a 0 "
			f"-- report it as unrated (policy: {settings.get('unrated_skill_policy') or 'Escalate'})."
		),
	}


def _applicant(job_applicant: str | None) -> dict[str, Any]:
	if not job_applicant:
		return {}
	row = frappe.db.get_value(
		"Job Applicant",
		job_applicant,
		[
			"name",
			"applicant_name",
			"email_id",
			"designation",
			"job_title",
			"status",
			"custom_interview_status",
			"custom_department",
			"resume_link",
		],
		as_dict=True,
	)
	if not row:
		return {"name": job_applicant, "note": "This Job Applicant no longer exists."}
	return {
		"name": row.name,
		"applicant_name": row.applicant_name,
		"email": row.email_id,
		"designation": row.designation,
		"job_opening": row.job_title,
		"status": row.status,
		"interview_status": row.custom_interview_status,
		"department": row.custom_department,
		"resume_link": row.resume_link,
	}


def _panel(doc) -> list[dict[str, Any]]:
	panel: list[dict[str, Any]] = []
	for row in doc.get("interview_details") or []:
		if not row.get("interviewer"):
			continue
		panel.append(
			{
				"user": row.interviewer,
				"name": row.get("custom_interviewer_name")
				or frappe.db.get_value("User", row.interviewer, "full_name"),
				"employee": row.get("custom_interviewer_employee"),
				"kind": "Internal",
				"confirmed": bool(cint(row.get("custom_is_confirm"))),
			}
		)
	for row in doc.get("custom_external_interviewers") or []:
		if not row.get("custom_user"):
			continue
		panel.append(
			{
				"user": row.custom_user,
				"name": row.get("user_name"),
				"kind": "External",
			}
		)
	return panel


def _feedback_records(doc, rubric: dict[str, Any]) -> dict[str, Any]:
	"""The Interview Feedback records already sitting against this interview.
	`prompt_hr.py.interview_availability.on_update` creates one draft per interviewer with
	the rubric rows pre-filled, so the normal state is 'drafts exist, waiting to be filled'
	-- not 'nothing exists yet'."""
	records: list[dict[str, Any]] = []
	writable = 0

	for row in frappe.get_all(
		"Interview Feedback",
		filters={"interview": doc.name},
		fields=["name", "interviewer", "result", "docstatus", "custom_obtained_average_score", "feedback"],
		order_by="creation",
	):
		assessments = frappe.get_all(
			"Skill Assessment",
			filters={"parent": row.name, "parenttype": "Interview Feedback"},
			fields=["name", "skill", "custom_rating_scale", "custom_rating_given"],
			order_by="idx",
		)
		rated = [a for a in assessments if cint(a.custom_rating_given)]
		state = {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(row.docstatus, "Draft")
		is_writable = row.docstatus == 0
		if is_writable:
			writable += 1

		records.append(
			{
				"name": row.name,
				"interviewer": row.interviewer,
				"state": state,
				"result": row.result,
				"obtained_score": flt(row.custom_obtained_average_score),
				"skills_on_record": [a.skill for a in assessments],
				"already_rated": [a.skill for a in rated],
				"has_written_feedback": bool((row.feedback or "").strip()),
				"writable": is_writable,
				"missing_rubric_skills": [
					s["skill"] for s in rubric["skills"] if s["skill"] not in {a.skill for a in assessments}
				],
			}
		)

	return {"records": records, "writable": writable}


# ==============================================================================
# WRITING
# ==============================================================================


def _validate_ratings(
	ratings: list[dict] | None,
	rubric: dict[str, Any],
	settings: dict[str, Any],
	allow_empty: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
	"""Every rating is checked against the rubric before anything is saved. A bad rating is
	refused with the reason, not clamped -- a silently adjusted score is worse than none."""
	by_skill = {s["skill"]: s for s in rubric["skills"]}
	by_key = {_key(s["skill"]): s for s in rubric["skills"]}
	require_evidence = cint(settings.get("require_evidence"))

	clean: list[dict[str, Any]] = []
	rejected: list[str] = []
	seen: set[str] = set()

	for entry in ratings or []:
		if not isinstance(entry, dict):
			rejected.append(f"{entry!r} is not an object with skill/rating_given/evidence")
			continue

		name = str(entry.get("skill") or "").strip()
		spec = by_skill.get(name) or by_key.get(_key(name))
		if not spec:
			rejected.append(
				f"{name or '(no skill)'} is not on the rubric for this round; rateable skills are "
				f"{', '.join(by_skill) or '(none)'}"
			)
			continue
		if spec["skill"] in seen:
			rejected.append(f"{spec['skill']} was rated twice")
			continue

		raw = entry.get("rating_given", entry.get("rating"))
		try:
			given = round(float(raw))
		except (TypeError, ValueError):
			rejected.append(f"{spec['skill']}: rating_given {raw!r} is not a number")
			continue

		scale = cint(spec["rating_scale"])
		if given < 0 or given > scale:
			rejected.append(f"{spec['skill']}: rating {given} is outside 0..{scale}")
			continue

		evidence = str(entry.get("evidence") or entry.get("quote") or "").strip()
		if require_evidence and not evidence:
			rejected.append(f"{spec['skill']}: no evidence quoted from the transcript")
			continue

		seen.add(spec["skill"])
		clean.append(
			{
				"skill": spec["skill"],
				"rating_given": given,
				"rating_scale": scale,
				"evidence": evidence,
				"comment": str(entry.get("comment") or "").strip(),
			}
		)

	if not clean and not rejected and not allow_empty:
		rejected.append(
			"no ratings were passed at all. If nothing could be rated, name the skills in "
			"`unrated` and say why in the summary -- that is a recordable outcome; an empty "
			"call is not"
		)
	return clean, rejected


def _feedback_body(
	settings: dict[str, Any],
	summary: str,
	clean: list[dict[str, Any]],
	unrated: list[str],
	recommendation: str | None,
) -> str:
	note = (settings.get("feedback_note") or "").strip()
	parts = [note, "", summary.strip()] if note else [summary.strip()]

	if clean:
		parts += ["", "Ratings and the evidence for them:"]
		for row in clean:
			line = f"- {row['skill']}: {row['rating_given']}/{row['rating_scale']}"
			if row["evidence"]:
				parts.append(f'{line} -- "{row["evidence"]}"')
			else:
				parts.append(line)
			if row["comment"]:
				parts.append(f"  {row['comment']}")

	if unrated and clean:
		parts += [
			"",
			"Not covered in the interview, so left unrated -- the interviewer needs to cover "
			f"these: {', '.join(unrated)}.",
		]
	elif unrated:
		parts += [
			"",
			f"Nothing on the rubric could be rated from this transcript: {', '.join(unrated)}. "
			"The reason is in the assessment above. This round still needs a human's judgement.",
		]

	if recommendation:
		parts += ["", f"Read of the outcome: {recommendation}. The result on this record is the interviewer's to set."]

	return "\n".join(parts).strip()


def _fill_drafts(
	doc, clean: list[dict[str, Any]], body: str, interviewer: str | None, settings: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	"""Write the ratings into the draft Interview Feedback records. Submitted and cancelled
	records are never touched, and neither is a draft an interviewer has already started
	unless overwriting is switched on."""
	overwrite = cint(settings.get("overwrite_existing_ratings"))
	default_scale = cint(settings.get("default_rating_scale")) or DEFAULT_RATING_SCALE
	by_key = {_key(r["skill"]): r for r in clean}

	filters: dict[str, Any] = {"interview": doc.name, "docstatus": 0}
	if interviewer:
		filters["interviewer"] = interviewer

	filled: list[dict[str, Any]] = []
	skipped: list[dict[str, Any]] = []

	for name in frappe.get_all("Interview Feedback", filters=filters, pluck="name", order_by="creation"):
		feedback = frappe.get_doc("Interview Feedback", name)

		already = [r for r in feedback.skill_assessment if cint(r.custom_rating_given)]
		if not overwrite and (already or (feedback.feedback or "").strip()):
			skipped.append(
				{
					"feedback": name,
					"interviewer": feedback.interviewer,
					"reason": "the interviewer has already entered ratings or a note; overwriting is off",
				}
			)
			continue

		applied: list[str] = []
		for row in feedback.skill_assessment:
			# ? A blank scale here is a ZeroDivisionError in
			# ? prompt_hr.py.interview_feedback.on_update, which divides by their sum.
			if not cint(row.custom_rating_scale):
				row.custom_rating_scale = default_scale
			rating = by_key.get(_key(row.skill))
			if not rating:
				continue
			row.custom_rating_given = min(cint(rating["rating_given"]), cint(row.custom_rating_scale))
			applied.append(row.skill)

		if not applied:
			skipped.append(
				{
					"feedback": name,
					"interviewer": feedback.interviewer,
					"reason": "none of the rated skills appear on this record's rows",
				}
			)
			continue

		feedback.feedback = body
		feedback.save()
		filled.append(
			{
				"feedback": name,
				"interviewer": feedback.interviewer,
				"skills_written": applied,
				"obtained_score": flt(
					frappe.db.get_value("Interview Feedback", name, "custom_obtained_average_score")
				),
				"state": "Draft",
			}
		)

	return filled, skipped


def _comment_on_interview(doc, body: str) -> str:
	"""Suggest Only mode. A Comment carries the assessment to whoever opens the Interview
	without touching a single field, so nothing downstream -- the Interview's status, the
	applicant's status, the feedback averages -- moves because the agent ran."""
	html = "<br>".join(frappe.utils.escape_html(line) for line in body.split("\n"))
	return doc.add_comment("Comment", html).name


def _short(text: str) -> str:
	text = " ".join(str(text or "").split())
	return text[:MAX_ATTEMPT_DETAIL] + ("..." if len(text) > MAX_ATTEMPT_DETAIL else "")
