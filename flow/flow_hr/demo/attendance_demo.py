# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Demo data for the Attendance Agent's dependency checks.

The agent's whole job is to notice that a leave request collides with something —
a milestone falling due while the person is away, half their team already out, a
release freeze, a leave balance that isn't there. On a fresh bench none of those
collisions exist to be noticed:

  * one submitted Leave Allocation for 74 employees, so every non-LWP balance is 0
  * every open Task's deadline is already in the past, so nothing is ever "due soon"
  * no Leave Block List at all, so no date is ever blocked
  * no future leave for anybody, so coverage is always 100%

`seed()` creates exactly those collisions for a small cohort and writes a manifest
of everything it touched. `clear()` reads the manifest back and undoes it —
deleting what it created and restoring the previous value of every field it changed.
Run `status()` to see what is currently seeded.

    bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.seed
    bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.status
    bench --site frappe.localhost execute flow.flow_hr.demo.attendance_demo.clear

Everything written here is ordinary application data created through the normal
document API, so every validation and hook runs exactly as it would for a human.
"""

from __future__ import annotations

import json
import os
from typing import Any

import frappe
from frappe.utils import add_days, add_to_date, cint, getdate, today

MANIFEST_FILENAME = "attendance_demo_manifest.json"

OPEN_TASK_STATUSES = (
	"Open",
	"Working",
	"Pending Review",
	"Overdue",
	"On Hold",
	"On Track",
	"Slight Delayed",
)

DEPARTMENT = "Development - STPL"
SHIFT_TYPE = "Day-018"

# ? ALLOCATIONS ROUGHLY MATCHING AN INDIAN IT SHOP'S ANNUAL ENTITLEMENT.
ALLOCATIONS = (
	("Casual Leave", 12),
	("Privilege Leave", 18),
	("Sick Leave", 10),
)

# ? RULES THAT EXIST AS FIELDS BUT ARE UNSET ON THIS BENCH, SO THE VALIDATIONS THAT
# ? READ THEM NEVER FIRE AND THE AGENT NEVER HAS A POLICY TO REPORT.
LEAVE_TYPE_RULES = {
	"Privilege Leave": {"custom_prior_days_required_for_applying_leave": 7},
	"Sick Leave": {"custom_require_attachment": 1},
}
HR_SETTINGS = {
	"custom_maximum_backdated_leave_days_including_today": 10,
	"custom_maximum_days_for_backdated_attendance_request": 7,
}

BLOCK_LIST_NAME = "Release Freeze - Sep 2026"
BLOCK_REASON = "Quarterly release freeze — no planned leave"


# ==============================================================================
# MANIFEST
# ==============================================================================


def _manifest_path() -> str:
	return os.path.join(frappe.get_site_path("private", "files"), MANIFEST_FILENAME)


def _load_manifest() -> dict[str, Any]:
	path = _manifest_path()
	if not os.path.exists(path):
		return {"created": [], "modified": [], "seeded_on": None}
	with open(path) as f:
		return json.load(f)


def _save_manifest(manifest: dict[str, Any]) -> None:
	path = _manifest_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as f:
		json.dump(manifest, f, indent=1, default=str)


class _Log:
	"""Records what was created and what was overwritten, so clear() can undo it."""

	def __init__(self, dry_run: bool):
		self.dry_run = dry_run
		self.created: list[dict[str, str]] = []
		self.modified: list[dict[str, Any]] = []
		self.notes: list[str] = []

	def created_doc(self, doctype: str, name: str) -> None:
		self.created.append({"doctype": doctype, "name": name})

	def set_value(self, doctype: str, name: str, values: dict[str, Any]) -> None:
		"""db_set a few fields, remembering their previous values first."""
		before = frappe.db.get_value(doctype, name, list(values.keys()), as_dict=True) or {}
		changed = {k: v for k, v in values.items() if before.get(k) != v}
		if not changed:
			return
		self.modified.append(
			{"doctype": doctype, "name": name, "before": {k: before.get(k) for k in changed}}
		)
		if not self.dry_run:
			for field, value in changed.items():
				frappe.db.set_value(doctype, name, field, value, update_modified=False)

	def note(self, message: str) -> None:
		self.notes.append(message)


# ==============================================================================
# SEED
# ==============================================================================


def seed(dry_run: int = 0, cohort_size: int = 14) -> dict[str, Any]:
	"""Create the collisions the Attendance Agent is supposed to catch.

	Pass dry_run=1 to see the plan — what would be created and changed — without
	writing anything.
	"""
	dry = bool(cint(dry_run))
	log = _Log(dry)

	company = frappe.db.get_value("Company", {}, "name")
	leave_period = frappe.db.get_value(
		"Leave Period", {"company": company, "is_active": 1}, ["name", "from_date", "to_date"], as_dict=True
	)
	if not leave_period:
		frappe.throw("No active Leave Period for this company — seed one first.")

	cohort, manager = _pick_cohort(cohort_size)
	if not cohort:
		frappe.throw(f"Found no Active {DEPARTMENT} employees with open tasks to build a cohort from.")

	summary: dict[str, Any] = {
		"dry_run": dry,
		"company": company,
		"leave_period": leave_period.name,
		"anchor_manager": manager,
		"cohort": [{"employee": c["name"], "employee_name": c["employee_name"]} for c in cohort],
	}

	# ? THE SEEDED LEAVE APPLICATIONS WOULD OTHERWISE FIRE THE ATTENDANCE AGENT ONCE EACH.
	# ? `in_migrate` IS THE SHORT-CIRCUIT flow.triggers.dispatch ALREADY HONOURS.
	previous_flag = frappe.flags.in_migrate
	frappe.flags.in_migrate = True
	try:
		summary["leave_type_rules"] = _seed_leave_type_rules(log)
		summary["hr_settings"] = _seed_hr_settings(log)
		summary["allocations"] = _seed_allocations(log, cohort, leave_period, company)
		summary["shift_assignments"] = _seed_shift_assignments(log, cohort, company)
		summary["task_replan"] = _seed_task_deadlines(log, cohort)
		summary["peer_leave"] = _seed_peer_leave(log, cohort, manager, company)
		summary["leave_block_list"] = _seed_leave_block_list(log, company)
		summary["checkins"] = _seed_checkins(log, cohort)
	finally:
		frappe.flags.in_migrate = previous_flag

	summary["created_count"] = len(log.created)
	summary["modified_count"] = len(log.modified)
	summary["notes"] = log.notes

	if dry:
		summary["would_create"] = log.created
		summary["would_modify"] = log.modified
		frappe.db.rollback()
		return summary

	manifest = _load_manifest()
	manifest["created"].extend(log.created)
	manifest["modified"].extend(log.modified)
	manifest["seeded_on"] = str(frappe.utils.now())
	_save_manifest(manifest)
	frappe.db.commit()

	summary["manifest"] = _manifest_path()
	return summary


def _pick_cohort(cohort_size: int) -> tuple[list[dict[str, Any]], str | None]:
	"""A cohort centred on one manager, so the immediate-team coverage check has teeth."""
	# ? A RELIEVING OR RESIGNATION DATE MEANS THE PERSON IS ON THEIR WAY OUT (OR CARRIES
	# ? STALE EXIT DATA). EITHER WAY THEY MAKE A CONFUSING DEMO SUBJECT, SO THEY ARE SKIPPED.
	candidates = frappe.get_all(
		"Employee",
		filters={
			"department": DEPARTMENT,
			"status": "Active",
			"user_id": ["is", "set"],
			"relieving_date": ["is", "not set"],
			"resignation_letter_date": ["is", "not set"],
		},
		fields=["name", "employee_name", "reports_to", "user_id", "date_of_joining"],
	)
	usable = _usable_task_counts([c["name"] for c in candidates])
	candidates = [c for c in candidates if usable.get(c["name"])]
	for c in candidates:
		c["usable_tasks"] = usable[c["name"]]

	by_manager: dict[str | None, list[dict[str, Any]]] = {}
	for c in candidates:
		by_manager.setdefault(c["reports_to"], []).append(c)

	# ? BIGGEST TEAM WITH A REAL MANAGER WINS; ITS PEERS ARE WHAT MAKES COVERAGE MOVE.
	ranked = sorted(
		((m, members) for m, members in by_manager.items() if m),
		key=lambda kv: len(kv[1]),
		reverse=True,
	)
	if not ranked:
		return sorted(candidates, key=lambda c: -c["usable_tasks"])[:cohort_size], None

	manager, team = ranked[0]
	cohort = sorted(team, key=lambda c: -c["usable_tasks"])[:cohort_size]
	if len(cohort) < cohort_size:
		spare = [c for c in candidates if c not in cohort]
		cohort += sorted(spare, key=lambda c: -c["usable_tasks"])[: cohort_size - len(cohort)]
	return cohort, manager


def _usable_task_counts(employees: list[str]) -> dict[str, int]:
	counts: dict[str, int] = {}
	for t in _usable_tasks(employees):
		counts[t["custom_employee"]] = counts.get(t["custom_employee"], 0) + 1
	return counts


def _usable_tasks(employees: list[str]) -> list[dict[str, Any]]:
	"""Open tasks whose deadline can legally be pushed into the next few weeks.

	erpnext refuses a task date past its project's expected end date, and past its
	parent task's expected end date, so tasks constrained that tightly are skipped
	rather than fought with.
	"""
	if not employees:
		return []
	tasks = frappe.get_all(
		"Task",
		filters={
			"custom_employee": ["in", employees],
			"status": ["in", OPEN_TASK_STATUSES],
			"parent_task": ["is", "not set"],
			"is_template": 0,
		},
		fields=[
			"name",
			"subject",
			"custom_employee",
			"project",
			"status",
			"priority",
			"is_milestone",
			"exp_start_date",
			"exp_end_date",
			"custom_planned_completion_date",
			"custom_project_manager",
		],
		limit=2000,
	)
	ceilings: dict[str, Any] = {}
	for p in frappe.get_all("Project", fields=["name", "expected_end_date"]):
		ceilings[p["name"]] = getdate(p["expected_end_date"]) if p["expected_end_date"] else None

	floor = getdate(add_days(today(), 3))
	usable = []
	for t in tasks:
		ceiling = ceilings.get(t["project"]) if t["project"] else None
		if ceiling is not None and ceiling < floor:
			continue
		t["ceiling"] = ceiling
		usable.append(t)
	return usable


def _seed_leave_type_rules(log: _Log) -> dict[str, Any]:
	applied = {}
	for leave_type, values in LEAVE_TYPE_RULES.items():
		if not frappe.db.exists("Leave Type", leave_type):
			log.note(f"Leave Type {leave_type} does not exist — rule skipped.")
			continue
		log.set_value("Leave Type", leave_type, values)
		applied[leave_type] = values
	return applied


def _seed_hr_settings(log: _Log) -> dict[str, Any]:
	log.set_value("HR Settings", "HR Settings", HR_SETTINGS)
	return HR_SETTINGS


def _seed_allocations(log: _Log, cohort, leave_period, company) -> list[dict[str, Any]]:
	out = []
	for emp in cohort:
		joined = getdate(emp["date_of_joining"])
		start = max(getdate(leave_period.from_date), joined)
		for leave_type, days in ALLOCATIONS:
			existing = frappe.db.exists(
				"Leave Allocation",
				{
					"employee": emp["name"],
					"leave_type": leave_type,
					"docstatus": 1,
					"from_date": ["<=", leave_period.to_date],
					"to_date": [">=", start],
				},
			)
			if existing:
				out.append({"employee": emp["name"], "leave_type": leave_type, "skipped": "already allocated"})
				continue
			if log.dry_run:
				out.append({"employee": emp["name"], "leave_type": leave_type, "new_leaves_allocated": days})
				log.created_doc("Leave Allocation", f"<new> {emp['name']} {leave_type}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Leave Allocation",
					"employee": emp["name"],
					"leave_type": leave_type,
					"from_date": start,
					"to_date": leave_period.to_date,
					"new_leaves_allocated": days,
					"leave_period": leave_period.name,
					"company": company,
				}
			)
			doc.insert(ignore_permissions=True)
			doc.submit()
			log.created_doc("Leave Allocation", doc.name)
			out.append(
				{
					"employee": emp["name"],
					"leave_type": leave_type,
					"name": doc.name,
					"total_leaves_allocated": doc.total_leaves_allocated,
				}
			)
	return out


def _seed_shift_assignments(log: _Log, cohort, company) -> list[dict[str, Any]]:
	if not frappe.db.exists("Shift Type", SHIFT_TYPE):
		log.note(f"Shift Type {SHIFT_TYPE} does not exist — shift assignments skipped.")
		return []
	out = []
	start = add_days(today(), -90)
	for emp in cohort:
		if frappe.db.exists("Shift Assignment", {"employee": emp["name"], "docstatus": 1}):
			out.append({"employee": emp["name"], "skipped": "already assigned"})
			continue
		if log.dry_run:
			log.created_doc("Shift Assignment", f"<new> {emp['name']}")
			out.append({"employee": emp["name"], "shift_type": SHIFT_TYPE, "start_date": str(start)})
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Shift Assignment",
				"employee": emp["name"],
				"shift_type": SHIFT_TYPE,
				"start_date": start,
				"status": "Active",
				"company": company,
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		log.created_doc("Shift Assignment", doc.name)
		out.append({"employee": emp["name"], "name": doc.name})
	return out


def _weekday(date):
	"""Nudge a date off Saturday/Sunday, so a seeded deadline never lands on a week off."""
	while date.weekday() >= 5:
		date = getdate(add_days(date, 1))
	return date


def _seed_task_deadlines(log: _Log, cohort) -> list[dict[str, Any]]:
	"""Give each cohort member a handful of live deadlines in the next few weeks.

	One is promoted to a High-priority milestone dated inside the conflict window, so
	a leave request over those dates has something concrete to collide with.
	"""
	out = []
	tasks_by_emp: dict[str, list[dict[str, Any]]] = {}
	for t in _usable_tasks([c["name"] for c in cohort]):
		tasks_by_emp.setdefault(t["custom_employee"], []).append(t)

	# ? THE WINDOW THE DEMO LEAVE REQUEST IS EXPECTED TO LAND ON.
	conflict_start = getdate(add_days(today(), 18))

	for index, emp in enumerate(cohort):
		tasks = tasks_by_emp.get(emp["name"], [])[:6]
		for offset, t in enumerate(tasks):
			# ? THE FIRST TASK LANDS SQUARELY INSIDE THE CONFLICT WINDOW (offset 0 -> day 0..2
			# ? of it); later ones fan out past it so the "due soon after they return" bucket
			# ? fills too.
			target = _weekday(getdate(add_days(conflict_start, offset * 3 + (index % 3))))
			if t["ceiling"] is not None and target > t["ceiling"]:
				target = t["ceiling"]
			if target <= getdate(today()):
				continue

			values: dict[str, Any] = {
				"exp_end_date": add_to_date(target, hours=18, as_datetime=True),
				"custom_planned_completion_date": add_to_date(target, hours=18, as_datetime=True),
			}
			start_floor = getdate(add_days(target, -10))
			if not t["exp_start_date"] or getdate(t["exp_start_date"]) > target:
				values["exp_start_date"] = add_to_date(start_floor, hours=10, as_datetime=True)

			if offset == 0:
				values["is_milestone"] = 1
				values["priority"] = "High"
				values["status"] = "Working"
			elif offset == 1:
				values["priority"] = "High"
				values["status"] = "Working"
			elif t["status"] == "Overdue":
				values["status"] = "Open"

			log.set_value("Task", t["name"], values)
			out.append(
				{
					"task": t["name"],
					"employee": emp["name"],
					"deadline": str(target),
					"milestone": bool(values.get("is_milestone")),
					"priority": values.get("priority", t["priority"]),
				}
			)
	return out


def _seed_peer_leave(log: _Log, cohort, manager, company) -> list[dict[str, Any]]:
	"""Put a few of the cohort's peers on approved leave over the same dates.

	Leave Without Pay is used deliberately: it needs no allocation, so this works
	whether or not the allocation step above ran.
	"""
	window_start = getdate(add_days(today(), 18))
	window_end = getdate(add_days(today(), 20))

	peers = [c for c in cohort if c["reports_to"] == manager][1:4] if manager else cohort[1:4]
	out = []
	for emp in peers:
		clash = frappe.db.exists(
			"Leave Application",
			{
				"employee": emp["name"],
				"docstatus": ["<", 2],
				"from_date": ["<=", window_end],
				"to_date": [">=", window_start],
			},
		)
		if clash:
			out.append({"employee": emp["name"], "skipped": f"already has {clash} in the window"})
			continue
		if log.dry_run:
			log.created_doc("Leave Application", f"<new> {emp['name']} LWP")
			out.append(
				{"employee": emp["name"], "from_date": str(window_start), "to_date": str(window_end)}
			)
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Leave Application",
				"employee": emp["name"],
				"leave_type": "Leave Without Pay",
				"from_date": window_start,
				"to_date": window_end,
				"description": "Pre-approved leave (demo data for the Attendance Agent)",
				"company": company,
				"posting_date": today(),
				"status": "Open",
				"workflow_state": "Pending",
			}
		)
		doc.insert(ignore_permissions=True)
		# ? hrms REFUSES TO SUBMIT A LEAVE APPLICATION WHOSE status IS STILL "Open",
		# ? SO status MUST LEAD THE WORKFLOW ACTION, NOT FOLLOW IT.
		doc.db_set("status", "Approved")
		doc.reload()
		from frappe.model.workflow import apply_workflow

		apply_workflow(doc, "Approve")
		log.created_doc("Leave Application", doc.name)
		out.append(
			{
				"employee": emp["name"],
				"name": doc.name,
				"from_date": str(window_start),
				"to_date": str(window_end),
				"state": doc.workflow_state,
			}
		)
	return out


def _seed_leave_block_list(log: _Log, company) -> dict[str, Any]:
	"""A release freeze inside the demo window, scoped to the cohort's department."""
	block_from = getdate(add_days(today(), 25))
	dates = [
		{"block_date": add_days(block_from, i), "reason": BLOCK_REASON}
		for i in range(5)
		if getdate(add_days(block_from, i)).weekday() < 5
	]

	if frappe.db.exists("Leave Block List", BLOCK_LIST_NAME):
		result = {"name": BLOCK_LIST_NAME, "skipped": "already exists"}
	elif log.dry_run:
		log.created_doc("Leave Block List", f"<new> {BLOCK_LIST_NAME}")
		result = {"name": BLOCK_LIST_NAME, "dates": [str(d["block_date"]) for d in dates]}
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Leave Block List",
				"leave_block_list_name": BLOCK_LIST_NAME,
				"company": company,
				"applies_to_all_departments": 0,
				"leave_block_list_dates": dates,
			}
		)
		doc.insert(ignore_permissions=True)
		log.created_doc("Leave Block List", doc.name)
		result = {"name": doc.name, "dates": [str(d["block_date"]) for d in dates]}

	# ? A BLOCK LIST ONLY BITES WHEN THE DEPARTMENT POINTS AT IT.
	if frappe.db.exists("Department", DEPARTMENT):
		log.set_value("Department", DEPARTMENT, {"leave_block_list": BLOCK_LIST_NAME})
		result["linked_to_department"] = DEPARTMENT
	else:
		log.note(f"Department {DEPARTMENT} not found — block list left unlinked.")
	return result


def _recent_working_days(holiday_list, count: int, skip_most_recent: int = 0) -> list[Any]:
	"""The `count` most recent working days before today, oldest first."""
	holidays = set()
	if holiday_list:
		holidays = {
			getdate(h)
			for h in frappe.get_all(
				"Holiday",
				filters={"parent": holiday_list, "holiday_date": ["between", [add_days(today(), -40), today()]]},
				pluck="holiday_date",
			)
		}
	days: list[Any] = []
	offset = 1 + skip_most_recent
	while len(days) < count and offset < 40:
		date = getdate(add_days(today(), -offset))
		if date not in holidays and date.weekday() < 5:
			days.append(date)
		offset += 1
	return list(reversed(days))


def _seed_checkins(log: _Log, cohort) -> list[dict[str, Any]]:
	"""Two clean days, one mispunch and one missing day, so the gaps are findable."""
	if not frappe.db.exists("Shift Type", SHIFT_TYPE):
		return []
	emp = cohort[0]
	out = []
	# ? TWO COMPLETE DAYS, THEN A MISPUNCH (IN WITH NO OUT). THE NEXT WORKING DAY IS LEFT
	# ? BARE ON PURPOSE, SO attendance_day_context HAS BOTH A MISPUNCH AND A REAL GAP.
	punch_plan = [
		[("IN", "10:02:00"), ("OUT", "19:14:00")],
		[("IN", "10:21:00"), ("OUT", "19:03:00")],
		[("IN", "10:07:00")],
	]
	holiday_list = frappe.db.get_value("Employee", emp["name"], "holiday_list")
	working_days = _recent_working_days(holiday_list, count=len(punch_plan), skip_most_recent=2)
	for date, punches in zip(working_days, punch_plan, strict=False):
		for log_type, clock in punches:
			stamp = f"{date} {clock}"
			if frappe.db.exists("Employee Checkin", {"employee": emp["name"], "time": stamp}):
				continue
			if log.dry_run:
				log.created_doc("Employee Checkin", f"<new> {emp['name']} {stamp}")
				out.append({"employee": emp["name"], "time": stamp, "log_type": log_type})
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Employee Checkin",
					"employee": emp["name"],
					"time": stamp,
					"log_type": log_type,
					"shift": SHIFT_TYPE,
				}
			)
			doc.insert(ignore_permissions=True)
			log.created_doc("Employee Checkin", doc.name)
			out.append({"employee": emp["name"], "name": doc.name, "time": stamp, "log_type": log_type})
	return out


# ==============================================================================
# STATUS AND CLEAR
# ==============================================================================


def status() -> dict[str, Any]:
	"""What the manifest says is currently seeded, and whether it is still there."""
	manifest = _load_manifest()
	present, missing = [], []
	for row in manifest["created"]:
		if frappe.db.exists(row["doctype"], row["name"]):
			present.append(row)
		else:
			missing.append(row)
	by_doctype: dict[str, int] = {}
	for row in present:
		by_doctype[row["doctype"]] = by_doctype.get(row["doctype"], 0) + 1
	return {
		"manifest": _manifest_path(),
		"seeded_on": manifest.get("seeded_on"),
		"created_still_present": by_doctype,
		"created_already_gone": missing,
		"fields_to_restore": len(manifest["modified"]),
	}


def clear(dry_run: int = 0) -> dict[str, Any]:
	"""Undo a seed: restore every changed field, then delete what was created."""
	dry = bool(cint(dry_run))
	manifest = _load_manifest()
	restored, deleted, failed = [], [], []

	previous_flag = frappe.flags.in_migrate
	frappe.flags.in_migrate = True
	try:
		# ? RESTORE FIELDS FIRST: A TASK'S OLD DATES MAY DEPEND ON NOTHING, BUT A
		# ? DEPARTMENT MUST STOP POINTING AT THE BLOCK LIST BEFORE IT CAN BE DELETED.
		for row in reversed(manifest["modified"]):
			if not frappe.db.exists(row["doctype"], row["name"]):
				continue
			if not dry:
				for field, value in row["before"].items():
					frappe.db.set_value(row["doctype"], row["name"], field, value, update_modified=False)
			restored.append({"doctype": row["doctype"], "name": row["name"], "fields": list(row["before"])})

		for row in reversed(manifest["created"]):
			if row["name"].startswith("<new>") or not frappe.db.exists(row["doctype"], row["name"]):
				continue
			if dry:
				deleted.append(row)
				continue
			try:
				doc = frappe.get_doc(row["doctype"], row["name"])
				if doc.meta.is_submittable and doc.docstatus == 1:
					doc.flags.ignore_permissions = True
					doc.cancel()
				frappe.delete_doc(
					row["doctype"], row["name"], force=True, ignore_permissions=True, delete_permanently=True
				)
				deleted.append(row)
			except Exception as e:
				failed.append({**row, "error": str(e)})
	finally:
		frappe.flags.in_migrate = previous_flag

	if dry:
		frappe.db.rollback()
		return {"dry_run": True, "would_restore": restored, "would_delete": deleted, "failed": failed}

	if not failed:
		path = _manifest_path()
		if os.path.exists(path):
			os.remove(path)
	else:
		remaining = [r for r in manifest["created"] if any(f["name"] == r["name"] for f in failed)]
		_save_manifest({"created": remaining, "modified": [], "seeded_on": manifest.get("seeded_on")})

	frappe.db.commit()
	return {
		"restored_fields_on": len(restored),
		"deleted": len(deleted),
		"failed": failed,
		"manifest_removed": not failed,
	}
