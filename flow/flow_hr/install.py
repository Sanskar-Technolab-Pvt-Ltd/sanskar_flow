# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Install the HR agent pack: the agents, their tools, their triggers and their knowledge.

Everything here is data rather than code, so it cannot live in a `.json` DocType file and be
picked up by `bench migrate`. It is described in `flow/flow_hr/fixtures/*.json` and written
into the site by `install()`.

`install()` runs from two places, because Frappe gives an app two different moments:

  * `after_install` in hooks.py -- a fresh `bench install-app flow`. Patches do not help here:
    `frappe.installer.install_app` calls `set_all_patches_as_completed()` before the
    `after_install` hooks, so an app's own patches.txt entries never execute on first install.
  * `flow.patches.v1_0.install_hr_agent_pack` -- every site that already has flow installed
    picks the pack up on the next `bench migrate`.

Both call the same function, and it is idempotent, so running it twice is harmless.

    bench --site <site> execute flow.flow_hr.install.install
    bench --site <site> execute flow.flow_hr.install.status
    bench --site <site> execute flow.flow_hr.install.uninstall

What `install()` rewrites every time, and what it writes only once, is a deliberate split:

    rewritten  agent instructions, tool descriptions, trigger prompt templates
    once only  whether an agent or a trigger is `enabled`, the transcript source list,
               API keys (never written at all), knowledge source content

The rewritten things are what gets tuned in this repo and should travel. The write-once
things are what the site's HR team owns, and clobbering them on every migrate would undo
their work -- including the deliberate decision to leave a trigger switched off.
"""

from __future__ import annotations

import json
import os
from typing import Any

import frappe
from frappe.utils import cint

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

SETTINGS_DOCTYPE = "Interview Feedback Agent Settings"

# The pack targets hrms recruitment and payroll. Without these there is nothing for the
# agents to act on, and a Flow Trigger pointing at a missing DocType fails link validation.
REQUIRED_DOCTYPES = ("Employee", "Job Applicant", "Job Offer", "Interview", "Leave Application")

# prompt_hr roles that should be able to read the agent's settings. Absent on a bench without
# prompt_hr, so they are granted only where they exist.
HR_READER_ROLES = ("S - HR Director (Global Admin)",)


def after_install() -> None:
	"""`after_install` hook. Never raises: a failure here would abort `bench install-app`."""
	if not _hr_stack_present():
		print(
			"flow: skipping the HR agent pack -- this site has no hrms recruitment doctypes yet.\n"
			"      Install hrms (and prompt_hr for the Employee Activation agent), then run:\n"
			"        bench --site <site> execute flow.flow_hr.install.install"
		)
		return
	try:
		install()
	except Exception:
		frappe.log_error(title="Flow HR agent pack install failed")
		print(
			"flow: the HR agent pack did not install cleanly -- see the Error Log.\n"
			"      Re-run it with: bench --site <site> execute flow.flow_hr.install.install"
		)


def install() -> dict[str, Any]:
	"""Create or refresh the whole pack. Safe to run repeatedly."""
	_require_flow()

	# The agents assign builtin tool slugs (read, create, execute, read_file). Those rows are
	# normally created by the after_migrate hook, which has not run yet on a fresh install --
	# and an agent's tool row pointing at a missing Flow Tool would be dropped.
	from flow.tools.builtins import sync_builtin_tools

	sync_builtin_tools()

	report = {
		"providers_and_models": _install_models(),
		"custom_fields": _install_custom_fields(),
		"tools": _install_tools(),
		"knowledge": _install_knowledge(),
		"agents": _install_agents(),
		"triggers": _install_triggers(),
		"settings": _install_settings(),
		"roles": _grant_hr_roles(),
	}
	frappe.db.commit()

	report["next_steps"] = _next_steps()
	print(frappe.as_json(report, indent=2))
	return report


def status() -> dict[str, Any]:
	"""What is installed right now, against what the fixtures describe."""
	agents = _fixture("agents.json")
	tools = _fixture("tools.json")
	triggers = _fixture("triggers.json")

	state = {
		"agents": frappe.get_all(
			"Flow Agent",
			filters={"name": ("in", [a["title"] for a in agents])},
			fields=["name", "model", "enabled", "max_iterations"],
			order_by="name",
		),
		"missing_agents": [
			a["title"] for a in agents if not frappe.db.exists("Flow Agent", a["title"])
		],
		"tools": frappe.get_all(
			"Flow Tool",
			filters={"name": ("in", [t["slug"] for t in tools])},
			fields=["name", "enabled", "requires_confirmation", "import_path"],
			order_by="name",
		),
		"missing_tools": [t["slug"] for t in tools if not frappe.db.exists("Flow Tool", t["slug"])],
		"triggers": frappe.get_all(
			"Flow Trigger",
			filters={"name": ("in", [t["name"] for t in triggers])},
			fields=["name", "event", "doc_event", "target_doctype", "enabled", "auto_approve"],
			order_by="name",
		),
		"missing_triggers": [
			t["name"] for t in triggers if not frappe.db.exists("Flow Trigger", t["name"])
		],
		"models_without_key": _models_without_key(),
	}

	if frappe.db.exists(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE):
		settings = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		state["interview_feedback_settings"] = {
			"enabled": cint(settings.enabled),
			"write_mode": settings.write_mode,
			"sources": [
				{"label": r.source_label, "type": r.source_type, "enabled": cint(r.enabled)}
				for r in settings.transcript_sources
			],
		}
	else:
		state["interview_feedback_settings"] = None

	print(frappe.as_json(state, indent=2))
	return state


def uninstall(keep_knowledge: bool = True, keep_custom_fields: bool = True) -> None:
	"""Remove the pack's agents, triggers and tools.

	Knowledge sources and the transcript custom fields are data by default -- somebody's
	policy document and somebody's interview transcript are in there.
	"""
	for trigger in _fixture("triggers.json"):
		if frappe.db.exists("Flow Trigger", trigger["name"]):
			frappe.delete_doc("Flow Trigger", trigger["name"], force=True)
	for agent in _fixture("agents.json"):
		if frappe.db.exists("Flow Agent", agent["title"]):
			frappe.delete_doc("Flow Agent", agent["title"], force=True)
	for tool in _fixture("tools.json"):
		if frappe.db.exists("Flow Tool", tool["slug"]):
			frappe.delete_doc("Flow Tool", tool["slug"], force=True)

	if not keep_knowledge:
		knowledge = _fixture("knowledge.json")
		for source in knowledge["sources"]:
			for name in frappe.get_all(
				"Flow Knowledge Source", filters={"title": source["title"]}, pluck="name"
			):
				frappe.delete_doc("Flow Knowledge Source", name, force=True)
		for base in knowledge["knowledge_bases"]:
			if frappe.db.exists("Flow Knowledge Base", base["title"]):
				frappe.delete_doc("Flow Knowledge Base", base["title"], force=True)

	if not keep_custom_fields:
		for doctype, fields in CUSTOM_FIELDS.items():
			for field in fields:
				name = f"{doctype}-{field['fieldname']}"
				if frappe.db.exists("Custom Field", name):
					frappe.delete_doc("Custom Field", name, force=True)

	frappe.db.commit()
	print("Flow HR agent pack removed.")


# ==============================================================================
# Custom fields
# ==============================================================================

# The transcript landing zone on Interview, and the Exit-tab button on Employee. Both are
# part of the pack rather than of prompt_hr: without them the agents have no way in.
CUSTOM_FIELDS: dict[str, list[dict[str, Any]]] = {
	"Interview": [
		{
			"fieldname": "custom_transcript_section",
			"label": "Interview Transcript",
			"fieldtype": "Section Break",
			"insert_after": "interview_summary",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_transcript_text",
			"label": "Transcript Text",
			"fieldtype": "Long Text",
			"insert_after": "custom_transcript_section",
			"description": "Paste the transcript here, or let the recorder's integration file it. Read by the Interview Feedback Agent.",
		},
		{
			"fieldname": "custom_transcript_file",
			"label": "Transcript File",
			"fieldtype": "Attach",
			"insert_after": "custom_transcript_text",
			"description": "A transcript export -- .vtt, .srt, .txt, .json, .docx or .pdf all read fine.",
		},
		{
			"fieldname": "custom_column_break_transcript",
			"fieldtype": "Column Break",
			"insert_after": "custom_transcript_file",
		},
		{
			"fieldname": "custom_transcript_external_ref",
			"label": "External Transcript Reference",
			"fieldtype": "Data",
			"insert_after": "custom_column_break_transcript",
			"description": "The recording's id in the tool that made it -- a Teams meeting id, a Zoom recording id, a vendor job id. An HTTP Endpoint transcript source can fetch by this without the text ever being stored here.",
		},
		{
			"fieldname": "custom_transcript_source",
			"label": "Transcript Filed By",
			"fieldtype": "Data",
			"insert_after": "custom_transcript_external_ref",
			"read_only": 1,
		},
		{
			"fieldname": "custom_transcript_received_on",
			"label": "Transcript Received On",
			"fieldtype": "Datetime",
			"insert_after": "custom_transcript_source",
			"read_only": 1,
		},
	],
	"Employee": [
		{
			"fieldname": "custom_start_offboarding",
			"label": "Start Offboarding",
			"fieldtype": "Button",
			# prompt_hr's own Exit tab field when it is there; otherwise the end of the form,
			# which is still on the Exit tab for stock hrms.
			"insert_after": "custom_is_notice_period_served",
			"description": "Sets the relieving date and hands the exit paperwork to the Offboarding Agent.",
		},
	],
}

# Where the Employee button goes when prompt_hr is not installed and its Exit-tab field
# does not exist.
EMPLOYEE_BUTTON_FALLBACK_ANCHOR = "relieving_date"


def _install_custom_fields() -> dict[str, list[str]]:
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	created: dict[str, list[str]] = {}
	for doctype, fields in CUSTOM_FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			created[doctype] = ["skipped: doctype not installed"]
			continue

		meta = frappe.get_meta(doctype)
		missing = []
		for field in fields:
			if frappe.db.exists("Custom Field", f"{doctype}-{field['fieldname']}"):
				continue
			field = dict(field)
			# An insert_after naming a field this site does not have silently drops the new
			# field to the bottom of the form, where nobody looks for it.
			if field.get("insert_after") and not meta.has_field(field["insert_after"]):
				if doctype == "Employee":
					field["insert_after"] = EMPLOYEE_BUTTON_FALLBACK_ANCHOR
				else:
					field.pop("insert_after")
			missing.append(field)

		if missing:
			create_custom_fields({doctype: missing}, ignore_validate=True)
		created[doctype] = [f["fieldname"] for f in missing]
	return created


# ==============================================================================
# Flow records
# ==============================================================================


def _install_models() -> dict[str, Any]:
	"""Create the providers and models the agents point at -- shape only.

	API keys are never in the repo and never written here. A model created without one is
	left disabled, so `status()` and the install report can name it instead of the agents
	failing at their first LLM call with an authentication error.
	"""
	data = _fixture("models.json")
	touched: dict[str, Any] = {"providers": [], "models": [], "needs_api_key": []}

	for provider in data["providers"]:
		if frappe.db.exists("Flow Provider", provider["provider"]):
			continue
		doc = frappe.new_doc("Flow Provider")
		doc.update({k: v for k, v in provider.items() if v is not None})
		doc.save(ignore_permissions=True)
		touched["providers"].append(f"{provider['provider']} created")

	for model in data["models"]:
		if frappe.db.exists("Flow Model", model["title"]):
			continue
		doc = frappe.new_doc("Flow Model")
		doc.update({k: v for k, v in model.items() if v is not None})
		# No key yet, so nothing should be routed to it.
		doc.enabled = 0
		doc.save(ignore_permissions=True)
		touched["models"].append(f"{model['title']} created (disabled -- no API key)")

	touched["needs_api_key"] = _models_without_key()
	return touched


def _models_without_key() -> list[str]:
	needs = []
	for model in _fixture("models.json")["models"]:
		if not frappe.db.exists("Flow Model", model["title"]):
			continue
		doc = frappe.get_doc("Flow Model", model["title"])
		if doc.get_password("api_key", raise_exception=False):
			continue
		if doc.provider and frappe.db.exists("Flow Provider", doc.provider):
			provider = frappe.get_doc("Flow Provider", doc.provider)
			if provider.get_password("api_key", raise_exception=False):
				continue
		needs.append(model["title"])
	return needs


def _install_tools() -> list[str]:
	"""Create or refresh the pack's tools.

	The description is the tool's entire interface as far as the model is concerned -- it is
	what the agent reads to decide whether and how to call it -- so it is rewritten from the
	fixture every time, the same way instructions are.
	"""
	touched = []
	for tool in _fixture("tools.json"):
		if frappe.db.exists("Flow Tool", tool["slug"]):
			doc = frappe.get_doc("Flow Tool", tool["slug"])
			action = "updated"
		else:
			doc = frappe.new_doc("Flow Tool")
			doc.slug = tool["slug"]
			action = "created"

		doc.update(
			{
				"title": tool["title"],
				"type": tool["type"],
				"import_path": tool["import_path"],
				"code": tool["code"],
				"summary": tool["summary"],
				"description": tool["description"],
				"requires_confirmation": cint(tool["requires_confirmation"]),
			}
		)
		if action == "created":
			doc.enabled = cint(tool["enabled"])
		doc.save(ignore_permissions=True)
		touched.append(f"{tool['slug']} {action}")
	return touched


def _install_knowledge() -> dict[str, list[str]]:
	"""Create the knowledge bases and their text sources.

	Content is written on creation only. A source that has been re-edited on the site, or
	re-chunked against a newer embedding model, must not be reset by a migrate.
	"""
	data = _fixture("knowledge.json")
	touched: dict[str, list[str]] = {"bases": [], "sources": []}

	for base in data["knowledge_bases"]:
		if frappe.db.exists("Flow Knowledge Base", base["title"]):
			continue
		doc = frappe.new_doc("Flow Knowledge Base")
		doc.update(base)
		doc.save(ignore_permissions=True)
		touched["bases"].append(f"{base['title']} created")

	for source in data["sources"]:
		if frappe.db.exists("Flow Knowledge Source", {"title": source["title"]}):
			continue
		if not frappe.db.exists("Flow Knowledge Base", source["knowledge_base"]):
			touched["sources"].append(f"{source['title']} skipped -- no knowledge base")
			continue
		doc = frappe.new_doc("Flow Knowledge Source")
		doc.update(source)
		doc.save(ignore_permissions=True)
		touched["sources"].append(f"{source['title']} created")

	return touched


def _install_agents() -> list[str]:
	"""Create or refresh the agents.

	`instructions` is the agent, so it is rewritten from the fixture every time. `enabled` is
	not: an agent switched off on this site was switched off for a reason.
	"""
	touched = []
	for agent in _fixture("agents.json"):
		title = agent["title"]
		if frappe.db.exists("Flow Agent", title):
			doc = frappe.get_doc("Flow Agent", title)
			action = "updated"
		else:
			doc = frappe.new_doc("Flow Agent")
			doc.title = title
			doc.enabled = cint(agent["enabled"])
			action = "created"

		if agent["model"] and frappe.db.exists("Flow Model", agent["model"]):
			doc.model = agent["model"]
		doc.max_iterations = agent["max_iterations"]
		doc.instructions = agent["instructions"]

		# A tool row pointing at a Flow Tool that does not exist fails link validation and
		# takes the whole agent down with it, so name what is missing instead.
		missing = [slug for slug in agent["tools"] if not frappe.db.exists("Flow Tool", slug)]
		doc.tools = []
		for slug in agent["tools"]:
			if slug not in missing:
				doc.append("tools", {"tool": slug})

		doc.knowledge_bases = []
		for base in agent["knowledge_bases"]:
			if frappe.db.exists("Flow Knowledge Base", base):
				doc.append("knowledge_bases", {"knowledge_base": base})

		doc.save(ignore_permissions=True)
		note = f"{title} {action}"
		if missing:
			note += f" (tools not installed: {', '.join(missing)})"
		if not agent["model"] or not frappe.db.exists("Flow Model", agent["model"]):
			note += f" (model {agent['model']!r} not on this site)"
		touched.append(note)
	return touched


def _install_triggers() -> list[str]:
	"""Create or refresh the triggers.

	`prompt_template` and `condition` travel with the repo. `enabled` does not: one of these
	triggers ships deliberately switched off, and any of them may have been switched off here.
	"""
	touched = []
	for trigger in _fixture("triggers.json"):
		name = trigger["name"]

		if not frappe.db.exists("Flow Agent", trigger["agent"]):
			touched.append(f"{name} skipped -- agent {trigger['agent']} not installed")
			continue
		target = trigger["target_doctype"]
		if target and not frappe.db.exists("DocType", target):
			touched.append(f"{name} skipped -- {target} is not installed on this site")
			continue

		if frappe.db.exists("Flow Trigger", name):
			doc = frappe.get_doc("Flow Trigger", name)
			action = "updated"
		else:
			doc = frappe.new_doc("Flow Trigger")
			doc.title = trigger["title"]
			doc.enabled = cint(trigger["enabled"])
			action = "created"

		doc.update(
			{
				"agent": trigger["agent"],
				"event": trigger["event"],
				"doc_event": trigger["doc_event"],
				"target_doctype": target,
				"cron_expression": trigger["cron_expression"],
				"auto_approve": cint(trigger["auto_approve"]),
				"run_as": trigger["run_as"] if frappe.db.exists("User", trigger["run_as"]) else "Administrator",
				"condition": trigger["condition"],
				"prompt_template": trigger["prompt_template"],
			}
		)
		doc.save(ignore_permissions=True)
		touched.append(f"{name} {action}")
	return touched


# ==============================================================================
# Interview Feedback Agent Settings
# ==============================================================================

# The starting source list. Deliberately only the sources that need no credentials and no
# provider knowledge, in the order that trusts a human's paste over a machine's export.
# Everything else -- Teams, Zoom, Meet, a note-taker's API -- is a row added later.
DEFAULT_TRANSCRIPT_SOURCES = [
	{
		"source_label": "Transcript text on Interview",
		"source_type": "Field on Interview",
		"fieldname": "custom_transcript_text",
		"transcript_format": "Auto",
		"notes": "What a recruiter pastes onto the Interview. First because a human put it there on purpose.",
	},
	{
		"source_label": "Transcript file on Interview",
		"source_type": "Field on Interview",
		"fieldname": "custom_transcript_file",
		"transcript_format": "Auto",
		"notes": "The Transcript File field. Handles .vtt/.srt/.txt/.json directly and pdf/docx through the extractor.",
	},
	{
		"source_label": "Transcript attachment on Interview",
		"source_type": "Attachment on Interview",
		"file_name_pattern": "*transcript*",
		"transcript_format": "Auto",
		"notes": "Any attachment whose name mentions 'transcript' -- what a meeting export usually lands as.",
	},
	{
		"source_label": "Caption file attached to Interview",
		"source_type": "Attachment on Interview",
		"file_name_pattern": "*.vtt",
		"transcript_format": "WebVTT",
		"notes": "Teams and Meet both export captions as .vtt. Add a *.srt row too if a recorder here emits SRT.",
	},
]

FEEDBACK_AGENT = "Interview Feedback Agent"
FEEDBACK_TRIGGER = "Interview Feedback Agent - Transcript Ready"


def _install_settings() -> dict[str, Any]:
	exists = frappe.db.exists(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
	doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE) if exists else frappe.new_doc(SETTINGS_DOCTYPE)

	if frappe.db.exists("Flow Agent", FEEDBACK_AGENT):
		doc.agent = FEEDBACK_AGENT
	if frappe.db.exists("Flow Trigger", FEEDBACK_TRIGGER):
		doc.trigger = FEEDBACK_TRIGGER
	if not exists:
		doc.enabled = 1

	# The source list is the one thing here that belongs to whoever configures this site:
	# which tool recorded the interview is not knowable from this repo. Seed it once; never
	# overwrite an edited list.
	seeded = 0
	if not doc.transcript_sources:
		for source in DEFAULT_TRANSCRIPT_SOURCES:
			doc.append("transcript_sources", {**source, "enabled": 1})
		seeded = len(DEFAULT_TRANSCRIPT_SOURCES)

	doc.save(ignore_permissions=True)
	return {"created": not exists, "sources_seeded": seeded}


def _grant_hr_roles() -> list[str]:
	"""Let prompt_hr's HR roles read the agent's settings, where those roles exist."""
	granted = []
	for role in HR_READER_ROLES:
		if not frappe.db.exists("Role", role):
			continue
		if frappe.db.exists("Custom DocPerm", {"parent": SETTINGS_DOCTYPE, "role": role}):
			continue
		frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": SETTINGS_DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": 0,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		granted.append(f"{role} may read {SETTINGS_DOCTYPE}")
	return granted


# ==============================================================================


def _fixture(filename: str) -> Any:
	with open(os.path.join(FIXTURES, filename)) as f:
		return json.load(f)


def _hr_stack_present() -> bool:
	return all(frappe.db.exists("DocType", doctype) for doctype in REQUIRED_DOCTYPES)


def _require_flow() -> None:
	for doctype in ("Flow Agent", "Flow Tool", "Flow Trigger", "Flow Model"):
		if not frappe.db.exists("DocType", doctype):
			frappe.throw(f"Flow is not migrated on this site yet -- {doctype} does not exist.")


def _next_steps() -> list[str]:
	steps = []
	missing_keys = _models_without_key()
	if missing_keys:
		steps.append(
			"Set an API key on these Flow Models (or on their Flow Provider) and enable them: "
			+ ", ".join(missing_keys)
		)
	if not frappe.db.exists("DocType", "Candidate Portal"):
		steps.append(
			"Install prompt_hr for the Employee Activation Agent -- its trigger watches "
			"Candidate Portal, which does not exist here."
		)
	disabled = frappe.get_all(
		"Flow Trigger",
		filters={"name": ("in", [t["name"] for t in _fixture("triggers.json")]), "enabled": 0},
		pluck="name",
	)
	if disabled:
		steps.append("These triggers are installed but switched off: " + ", ".join(disabled))
	steps.append(
		"Attach the HR policy documents to the 'HR Policies' knowledge base -- only the FAQ "
		"text sources travel with this repo."
	)
	return steps
