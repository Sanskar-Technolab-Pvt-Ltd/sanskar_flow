# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Flow tools for attendance and leave.

Registered as Flow Tool rows of type "Imported":
    leave_impact_report   -> flow.flow_hr.tools.attendance.leave_impact_report
    attendance_day_context -> flow.flow_hr.tools.attendance.attendance_day_context

Both are strictly read-only. They exist so the Attendance Agent does not have to
discover this install's dependency graph one `read` call at a time: a leave request
here is entangled with the employee's open Tasks and their planned completion dates,
the Projects they own, those projects' managers, the rest of their department's
already-approved leave, the holiday list and week-off pattern, the leave allocation
balance, and a dozen prompt_hr validations that fire on save or submit.

Nothing here inserts, updates or submits anything, and nothing here decides. The
report states the facts and names the blockers; the decision stays with the agent
and, where the report says so, with a human.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any

import frappe
from frappe.utils import add_days, cint, date_diff, flt, getdate, today

# ? TASK/PROJECT STATUSES THAT STILL REPRESENT OUTSTANDING WORK ON THIS INSTALL.
# ? "Template" and "Cancelled"/"Completed" are excluded; the rest all mean "not done".
OPEN_TASK_STATUSES = (
	"Open",
	"Working",
	"Pending Review",
	"Overdue",
	"On Hold",
	"On Track",
	"Slight Delayed",
)
CLOSED_PROJECT_STATUSES = ("Completed", "Cancelled")

# ? A LEAVE APPLICATION THAT ACTUALLY BLOCKS A DATE: submitted-and-approved, or still
# ? awaiting a decision. Rejected and Cancelled ones free the date up again.
BLOCKING_LEAVE_STATES = ("Pending", "Approved")

HIGH_PRIORITIES = ("High", "Urgent")


# ==============================================================================
# LEAVE IMPACT REPORT
# ==============================================================================


def leave_impact_report(
	leave_application: Annotated[
		str | None,
		"Leave Application ID, e.g. HR-LAP-2026-00037. Pass this when reacting to a "
		"filed application — every other argument is then read off the record.",
	] = None,
	employee: Annotated[
		str | None,
		"Employee ID, e.g. ST-EMP-00012. Never a person's name. Required only when "
		"leave_application is not given (i.e. for a what-if check before anything is filed).",
	] = None,
	from_date: Annotated[str | None, "First day of leave, YYYY-MM-DD. Only for a what-if check."] = None,
	to_date: Annotated[str | None, "Last day of leave, YYYY-MM-DD. Only for a what-if check."] = None,
	leave_type: Annotated[
		str | None,
		"Leave Type, e.g. Casual Leave. Only for a what-if check.",
	] = None,
	lookahead_days: Annotated[
		int,
		"How many days past the last day of leave still count as 'due immediately after "
		"they get back'. Defaults to 7.",
	] = 7,
) -> dict[str, Any]:
	"""Assemble the full dependency picture for one leave request, without changing anything."""
	frappe.has_permission("Leave Application", "read", throw=True)

	req = _resolve_request(leave_application, employee, from_date, to_date, leave_type)
	emp = _employee_context(req["employee"])

	f_date, t_date = req["from_date"], req["to_date"]
	holiday_list = emp["holiday_list"]

	holidays = _holidays_in(holiday_list, f_date, t_date)
	holiday_dates = {h["date"] for h in holidays}
	all_dates = _daterange(f_date, t_date)
	working_dates = [d for d in all_dates if d not in holiday_dates]

	lt_rules = _leave_type_rules(req["leave_type"])
	balance = _leave_balance(req["employee"], req["leave_type"], f_date, t_date, req["total_leave_days"], lt_rules)
	own = _own_conflicts(req["employee"], f_date, t_date, req.get("name"))
	work = _work_dependencies(req["employee"], f_date, t_date, lookahead_days)
	coverage = _team_coverage(emp, f_date, t_date, holiday_dates, req.get("name"))
	shift = _shift_context(req["employee"], f_date, t_date, emp)
	blackout = _blackout_dates(emp, f_date, t_date)

	# ? THREE BUCKETS, AND THE DIFFERENCE MATTERS. A *blocker* means this install's own
	# ? validations would refuse the save or submit. A *concern* is a real conflict a human
	# ? has to weigh. A *note* is background a manager should see but which decides nothing —
	# ? keeping those out of `concerns` is what stops every report reading as "needs review".
	blockers: list[dict[str, str]] = []
	concerns: list[dict[str, str]] = []
	notes: list[dict[str, str]] = []

	_check_employee_state(emp, req, blockers, concerns, notes)
	_check_leave_type_rules(req, emp, lt_rules, balance, len(working_dates), blockers, concerns, notes)
	_check_dates(req, emp, lt_rules, blackout, blockers, concerns)
	_check_own_conflicts(own, blockers, concerns)
	_check_work(work, concerns, notes)
	_check_coverage(coverage, concerns)

	verdict = "blocked" if blockers else ("needs_review" if concerns else "clean")

	return {
		"verdict": verdict,
		"recommendation": _recommendation(verdict, blockers, concerns, req),
		"predicted_blockers": blockers,
		"concerns": concerns,
		"notes": notes,
		"leave_application": req.get("name"),
		"request": {
			"employee": req["employee"],
			"employee_name": emp["employee_name"],
			"leave_type": req["leave_type"],
			"from_date": str(f_date),
			"to_date": str(t_date),
			"total_leave_days": req["total_leave_days"],
			"half_day": req.get("half_day"),
			"half_day_date": str(req["half_day_date"]) if req.get("half_day_date") else None,
			"status": req.get("status"),
			"workflow_state": req.get("workflow_state"),
			"posting_date": str(req["posting_date"]) if req.get("posting_date") else None,
			"description": req.get("description"),
			"has_attachment": bool(req.get("custom_attachment")),
			"calendar_days": len(all_dates),
			"working_days": len(working_dates),
			"holidays_in_window": holidays,
		},
		"employee": emp,
		"leave_balance": balance,
		"leave_type_rules": lt_rules,
		"own_conflicts": own,
		"work_dependencies": work,
		"team_coverage": coverage,
		"shift": shift,
		"blackout": blackout,
		"notify": _notify_targets(emp, req, work),
	}


# ==============================================================================
# ATTENDANCE DAY CONTEXT
# ==============================================================================


def attendance_day_context(
	employee: Annotated[str, "Employee ID, e.g. ST-EMP-00012. Never a person's name."],
	from_date: Annotated[str, "First date to examine, YYYY-MM-DD."],
	to_date: Annotated[str | None, "Last date to examine, YYYY-MM-DD. Defaults to from_date."] = None,
) -> dict[str, Any]:
	"""Report, date by date, what attendance already exists for an employee and what is missing."""
	frappe.has_permission("Attendance", "read", throw=True)

	emp = _employee_context(employee)
	f_date = getdate(from_date)
	t_date = getdate(to_date) if to_date else f_date
	if t_date < f_date:
		f_date, t_date = t_date, f_date

	holidays = {h["date"]: h for h in _holidays_in(emp["holiday_list"], f_date, t_date)}

	attendance = {
		getdate(a["attendance_date"]): a
		for a in frappe.get_all(
			"Attendance",
			filters={"employee": employee, "attendance_date": ["between", [f_date, t_date]], "docstatus": ["<", 2]},
			fields=[
				"name",
				"attendance_date",
				"status",
				"docstatus",
				"shift",
				"leave_type",
				"custom_type",
				"custom_work_hours",
				"custom_checkin_time",
				"custom_checkout_time",
				"custom_remarks",
			],
		)
	}

	leaves = frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee,
			"docstatus": ["<", 2],
			"workflow_state": ["in", BLOCKING_LEAVE_STATES],
			"from_date": ["<=", t_date],
			"to_date": [">=", f_date],
		},
		fields=["name", "leave_type", "from_date", "to_date", "workflow_state", "status"],
	)

	requests = frappe.get_all(
		"Attendance Request",
		filters={
			"employee": employee,
			"docstatus": ["<", 2],
			"from_date": ["<=", t_date],
			"to_date": [">=", f_date],
		},
		fields=["name", "reason", "from_date", "to_date", "workflow_state", "custom_status"],
	)

	checkins: dict[datetime.date, list[dict[str, Any]]] = {}
	for row in frappe.get_all(
		"Employee Checkin",
		filters={"employee": employee, "time": ["between", [f"{f_date} 00:00:00", f"{t_date} 23:59:59"]]},
		fields=["name", "time", "log_type", "shift", "attendance"],
		order_by="time asc",
	):
		checkins.setdefault(getdate(row["time"]), []).append(
			{"name": row["name"], "time": str(row["time"]), "log_type": row["log_type"], "attendance": row["attendance"]}
		)

	tasks_by_date = _tasks_by_deadline(employee, f_date, t_date)

	backdated_limit = cint(
		frappe.db.get_single_value("HR Settings", "custom_maximum_days_for_backdated_attendance_request")
	)

	days = []
	for d in _daterange(f_date, t_date):
		holiday = holidays.get(d)
		att = attendance.get(d)
		leave = next((l for l in leaves if getdate(l["from_date"]) <= d <= getdate(l["to_date"])), None)
		request = next((r for r in requests if getdate(r["from_date"]) <= d <= getdate(r["to_date"])), None)
		punches = checkins.get(d, [])

		days.append(
			{
				"date": str(d),
				"weekday": d.strftime("%A"),
				"is_holiday": bool(holiday),
				"is_week_off": bool(holiday and holiday.get("weekly_off")),
				"holiday_description": holiday.get("description") if holiday else None,
				"attendance": _compact(att, ("name", "status", "docstatus", "shift", "leave_type", "custom_type", "custom_work_hours", "custom_checkin_time", "custom_checkout_time", "custom_remarks")),
				"leave_application": _compact(leave, ("name", "leave_type", "workflow_state", "status")),
				"attendance_request": _compact(request, ("name", "reason", "workflow_state", "custom_status")),
				"checkins": punches,
				"checkin_count": len(punches),
				"mispunch": len(punches) == 1,
				"tasks_due": tasks_by_date.get(d, []),
				"gap": not holiday and not att and not leave and not request,
				"days_in_past": date_diff(today(), d),
			}
		)

	gaps = [d["date"] for d in days if d["gap"]]
	mispunches = [d["date"] for d in days if d["mispunch"]]

	# ? BACKDATED WINDOW IS 0 WHEN UNSET, WHICH ON THIS SITE MEANS "NO LIMIT CONFIGURED".
	stale_gaps = (
		[d["date"] for d in days if d["gap"] and d["days_in_past"] > backdated_limit] if backdated_limit else []
	)

	return {
		"employee": employee,
		"employee_name": emp["employee_name"],
		"department": emp["department"],
		"reports_to": emp["reports_to"],
		"reports_to_name": emp["reports_to_name"],
		"holiday_list": emp["holiday_list"],
		"default_shift": emp["default_shift"],
		"from_date": str(f_date),
		"to_date": str(t_date),
		"days": days,
		"summary": {
			"unaccounted_dates": gaps,
			"mispunch_dates": mispunches,
			"dates_beyond_backdated_window": stale_gaps,
			"backdated_attendance_request_limit_days": backdated_limit or None,
			"already_marked": [d["date"] for d in days if d["attendance"]],
			"on_leave": [d["date"] for d in days if d["leave_application"]],
			"holidays": [d["date"] for d in days if d["is_holiday"]],
		},
		"shift": _shift_context(employee, f_date, t_date, emp),
		"notify": _notify_targets(emp, {"employee": employee}, {"project_manager_emails": []}),
	}


# ==============================================================================
# REQUEST / EMPLOYEE RESOLUTION
# ==============================================================================


def _resolve_request(leave_application, employee, from_date, to_date, leave_type) -> dict[str, Any]:
	if leave_application:
		doc = frappe.get_doc("Leave Application", leave_application)
		doc.check_permission("read")
		return {
			"name": doc.name,
			"employee": doc.employee,
			"leave_type": doc.leave_type,
			"from_date": getdate(doc.from_date),
			"to_date": getdate(doc.to_date),
			"total_leave_days": flt(doc.total_leave_days),
			"half_day": cint(doc.half_day),
			"half_day_date": doc.half_day_date,
			"status": doc.status,
			"workflow_state": doc.workflow_state,
			"posting_date": doc.posting_date,
			"description": doc.description,
			"custom_attachment": doc.custom_attachment,
			"leave_approver": doc.leave_approver,
			"existing_project_details": [
				{
					"project": r.project,
					"project_manager": r.project_manager,
					"task_description": r.task_description,
					"approve_status": r.approve_status,
				}
				for r in (doc.custom_project_details or [])
			],
		}

	if not (employee and from_date and to_date and leave_type):
		frappe.throw(
			"Pass either leave_application, or all of employee, from_date, to_date and leave_type.",
			title="Not enough to work with",
		)

	f_date, t_date = getdate(from_date), getdate(to_date)
	if t_date < f_date:
		f_date, t_date = t_date, f_date
	return {
		"name": None,
		"employee": employee,
		"leave_type": leave_type,
		"from_date": f_date,
		"to_date": t_date,
		"total_leave_days": flt(date_diff(t_date, f_date) + 1),
		"half_day": 0,
		"half_day_date": None,
		"status": None,
		"workflow_state": None,
		# ? A WHAT-IF IS BEING ASKED NOW, SO "NOW" IS THE FILING DATE THE NOTICE-PERIOD AND
		# ? BACKDATING RULES SHOULD BE MEASURED AGAINST. LEAVING IT None SILENTLY SKIPS THEM.
		"posting_date": getdate(today()),
		"description": None,
		"custom_attachment": None,
		"leave_approver": None,
		"existing_project_details": [],
	}


def _employee_context(employee: str) -> dict[str, Any]:
	if not frappe.db.exists("Employee", employee):
		frappe.throw(
			f"No Employee {employee}. Employee IDs on this site look like ST-EMP-00012 — "
			"read Employee filtered by employee_name to find the ID from a person's name.",
			title="Unknown Employee",
		)

	e = frappe.db.get_value(
		"Employee",
		employee,
		[
			"name",
			"employee_name",
			"company",
			"department",
			"designation",
			"grade",
			"employment_type",
			"status",
			"date_of_joining",
			"relieving_date",
			"resignation_letter_date",
			"holiday_list",
			"default_shift",
			"leave_approver",
			"reports_to",
			"user_id",
			"personal_email",
			"company_email",
			"custom_probation_status",
			"custom_probation_end_date",
			"custom_weeklyoff",
			"custom_leave_policy",
		],
		as_dict=True,
	)

	rm: dict[str, Any] = {}
	if e.reports_to:
		rm = (
			frappe.db.get_value(
				"Employee",
				e.reports_to,
				["employee_name", "user_id", "personal_email", "company_email", "designation"],
				as_dict=True,
			)
			or {}
		)

	return {
		"employee": e.name,
		"employee_name": e.employee_name,
		"company": e.company,
		"department": e.department,
		"designation": e.designation,
		"grade": e.grade,
		"employment_type": e.employment_type,
		"status": e.status,
		"date_of_joining": str(e.date_of_joining) if e.date_of_joining else None,
		"relieving_date": str(e.relieving_date) if e.relieving_date else None,
		"resignation_letter_date": str(e.resignation_letter_date) if e.resignation_letter_date else None,
		"is_on_notice_period": bool(e.resignation_letter_date),
		"probation_status": e.custom_probation_status,
		"probation_end_date": str(e.custom_probation_end_date) if e.custom_probation_end_date else None,
		"holiday_list": e.holiday_list,
		"week_off_pattern": e.custom_weeklyoff,
		"default_shift": e.default_shift,
		"leave_policy": e.custom_leave_policy,
		"leave_approver": e.leave_approver,
		"reports_to": e.reports_to,
		"reports_to_name": rm.get("employee_name"),
		"reports_to_designation": rm.get("designation"),
		"reports_to_email": _first_email(rm.get("user_id"), rm.get("company_email"), rm.get("personal_email")),
		"email": _first_email(e.personal_email, e.company_email, e.user_id),
	}


# ==============================================================================
# CALENDAR
# ==============================================================================


def _daterange(f_date, t_date) -> list[datetime.date]:
	return [add_days(f_date, i) for i in range(date_diff(t_date, f_date) + 1)]


def _holidays_in(holiday_list: str | None, f_date, t_date) -> list[dict[str, Any]]:
	if not holiday_list:
		return []
	return [
		{
			"date": getdate(h["holiday_date"]),
			"description": h["description"],
			"weekly_off": bool(h["weekly_off"]),
		}
		for h in frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "holiday_date": ["between", [f_date, t_date]]},
			fields=["holiday_date", "description", "weekly_off"],
			order_by="holiday_date asc",
		)
	]


def _blackout_dates(emp: dict[str, Any], f_date, t_date) -> dict[str, Any]:
	"""Leave Block List dates inside the window that actually apply to this employee.

	A list applies when it is marked `applies_to_all_departments`, or when the
	employee's Department points at it via `Department.leave_block_list`. Employees
	named in the list's `leave_block_list_allowed` table may book the date anyway,
	so that exemption is reported rather than silently ignored.
	"""
	lists = frappe.get_all(
		"Leave Block List",
		filters={"company": emp["company"]},
		fields=["name", "leave_block_list_name", "applies_to_all_departments", "leave_type"],
	)
	if not lists:
		return {"blocked_dates": [], "lists_checked": [], "employee_is_exempt": False}

	department_list = (
		frappe.db.get_value("Department", emp["department"], "leave_block_list") if emp["department"] else None
	)
	applicable = [
		l["name"] for l in lists if l["applies_to_all_departments"] or l["name"] == department_list
	]
	if not applicable:
		return {"blocked_dates": [], "lists_checked": [], "employee_is_exempt": False}

	exempt = bool(
		emp["email"]
		and frappe.db.exists(
			"Leave Block List Allow", {"parent": ["in", applicable], "allow_user": emp["email"]}
		)
	)

	blocked = [
		{"date": str(getdate(r["block_date"])), "reason": r["reason"], "list": r["parent"]}
		for r in frappe.get_all(
			"Leave Block List Date",
			filters={"parent": ["in", applicable], "block_date": ["between", [f_date, t_date]]},
			fields=["parent", "block_date", "reason"],
			order_by="block_date asc",
		)
	]
	return {
		"blocked_dates": [] if exempt else blocked,
		"lists_checked": applicable,
		"employee_is_exempt": exempt,
		"blocked_dates_waived_by_exemption": blocked if exempt else [],
	}


def _shift_context(employee: str, f_date, t_date, emp: dict[str, Any]) -> dict[str, Any]:
	# ? AN OPEN-ENDED ASSIGNMENT (end_date NULL) STILL COVERS THE WINDOW, HENCE THE OR.
	covering = frappe.get_all(
		"Shift Assignment",
		filters={"employee": employee, "docstatus": 1, "start_date": ["<=", t_date]},
		or_filters=[["end_date", ">=", f_date], ["end_date", "is", "not set"]],
		fields=["name", "shift_type", "start_date", "end_date", "status"],
		order_by="start_date desc",
		limit=5,
	)
	covering = _stringify_dates(covering)
	shift_type = (covering[0]["shift_type"] if covering else None) or emp["default_shift"]
	timings = {}
	if shift_type:
		timings = (
			frappe.db.get_value(
				"Shift Type", shift_type, ["start_time", "end_time", "enable_auto_attendance"], as_dict=True
			)
			or {}
		)
	return {
		"shift_type": shift_type,
		"source": "Shift Assignment" if covering else ("Employee.default_shift" if shift_type else None),
		"start_time": str(timings.get("start_time")) if timings.get("start_time") else None,
		"end_time": str(timings.get("end_time")) if timings.get("end_time") else None,
		"auto_attendance": bool(timings.get("enable_auto_attendance")),
		"assignments_covering_window": covering,
	}


# ==============================================================================
# BALANCE AND LEAVE TYPE RULES
# ==============================================================================


def _leave_type_rules(leave_type: str) -> dict[str, Any]:
	lt = (
		frappe.db.get_value(
			"Leave Type",
			leave_type,
			[
				"name",
				"is_lwp",
				"include_holiday",
				"is_compensatory",
				"is_earned_leave",
				"max_continuous_days_allowed",
				"max_leaves_allowed",
				"custom_prior_days_required_for_applying_leave",
				"custom_require_attachment",
				"custom_allow_for_employees_who_are_on_notice_period",
				"custom_is_festival_leave",
				"custom_maximum_times_for_applying_leave",
			],
			as_dict=True,
		)
		or {}
	)
	if not lt:
		frappe.throw(f"No Leave Type {leave_type}.", title="Unknown Leave Type")
	return {
		"leave_type": lt.name,
		"is_lwp": bool(lt.is_lwp),
		"counts_holidays_as_leave": bool(lt.include_holiday),
		"is_compensatory": bool(lt.is_compensatory),
		"is_earned_leave": bool(lt.is_earned_leave),
		"max_continuous_days_allowed": cint(lt.max_continuous_days_allowed) or None,
		"max_leaves_allowed": flt(lt.max_leaves_allowed) or None,
		"prior_days_required": cint(lt.custom_prior_days_required_for_applying_leave) or None,
		"attachment_required": bool(lt.custom_require_attachment),
		"allowed_during_notice_period": bool(lt.custom_allow_for_employees_who_are_on_notice_period),
		"is_festival_leave": bool(lt.custom_is_festival_leave),
		"max_applications_per_period": lt.custom_maximum_times_for_applying_leave or None,
	}


def _leave_balance(employee, leave_type, f_date, t_date, days_requested, lt_rules) -> dict[str, Any]:
	allocations = frappe.get_all(
		"Leave Allocation",
		filters={
			"employee": employee,
			"leave_type": leave_type,
			"docstatus": 1,
			"from_date": ["<=", t_date],
			"to_date": [">=", f_date],
		},
		fields=["name", "from_date", "to_date", "total_leaves_allocated", "new_leaves_allocated", "carry_forward"],
	)

	balance = None
	consumable = None
	error = None
	try:
		result = frappe.call(
			"sanskar_erp.api.hooks.doctype.leave_application.custom_get_leave_balance_on",
			employee=employee,
			leave_type=leave_type,
			date=f_date,
			to_date=t_date,
			consider_all_leaves_in_the_allocation_period=True,
			for_consumption=True,
		)
		if isinstance(result, dict):
			balance = flt(result.get("leave_balance"))
			consumable = flt(result.get("leave_balance_for_consumption"))
		else:
			balance = flt(result)
			consumable = balance
	except Exception as e:  # ? A BROKEN BALANCE CALC MUST NOT TAKE THE WHOLE REPORT DOWN.
		error = str(e)
		frappe.log_error(frappe.get_traceback(), "leave_impact_report: balance lookup failed")

	return {
		"leave_type": leave_type,
		"is_lwp": lt_rules["is_lwp"],
		"allocation_found": bool(allocations),
		"allocations": allocations,
		"balance_before": balance,
		"consumable_now": consumable,
		"days_requested": flt(days_requested),
		"balance_after": (flt(balance) - flt(days_requested)) if balance is not None else None,
		"sufficient": True if lt_rules["is_lwp"] else (consumable is not None and consumable >= flt(days_requested)),
		"source": "sanskar_erp.api.hooks.doctype.leave_application.custom_get_leave_balance_on",
		"error": error,
	}


# ==============================================================================
# THE EMPLOYEE'S OWN CLASHES
# ==============================================================================


def _own_conflicts(employee, f_date, t_date, exclude_leave_application) -> dict[str, Any]:
	la_filters = {
		"employee": employee,
		"docstatus": ["<", 2],
		"workflow_state": ["in", BLOCKING_LEAVE_STATES],
		"from_date": ["<=", t_date],
		"to_date": [">=", f_date],
	}
	if exclude_leave_application:
		la_filters["name"] = ["!=", exclude_leave_application]

	overlapping = frappe.get_all(
		"Leave Application",
		filters=la_filters,
		fields=[
			"name",
			"leave_type",
			"from_date",
			"to_date",
			"total_leave_days",
			"status",
			"workflow_state",
			"leave_approver",
		],
		order_by="from_date asc",
	)

	attendance = frappe.get_all(
		"Attendance",
		filters={
			"employee": employee,
			"attendance_date": ["between", [f_date, t_date]],
			"docstatus": ["<", 2],
		},
		fields=["name", "attendance_date", "status", "docstatus", "leave_type", "custom_type"],
		order_by="attendance_date asc",
	)

	requests = frappe.get_all(
		"Attendance Request",
		filters={
			"employee": employee,
			"docstatus": ["<", 2],
			"from_date": ["<=", t_date],
			"to_date": [">=", f_date],
		},
		fields=["name", "reason", "from_date", "to_date", "workflow_state", "custom_status"],
	)

	return {
		"overlapping_leave_applications": _stringify_dates(overlapping),
		"attendance_already_marked": _stringify_dates(attendance),
		"overlapping_attendance_requests": _stringify_dates(requests),
	}


# ==============================================================================
# WORK DEPENDENCIES — THE POINT OF THE WHOLE REPORT
# ==============================================================================


def _task_deadline(t: dict[str, Any]):
	raw = t.get("custom_planned_completion_date") or t.get("exp_end_date")
	return getdate(raw) if raw else None


def _task_row(t: dict[str, Any], deadline) -> dict[str, Any]:
	return {
		"task": t["name"],
		"subject": t["subject"],
		"status": t["status"],
		"priority": t["priority"],
		"is_milestone": bool(t["is_milestone"]),
		"progress": flt(t["progress"]),
		"project": t["project"],
		"project_manager": t["custom_project_manager"],
		"task_owner": t["custom_task_owner"],
		"deadline": str(deadline) if deadline else None,
		"deadline_field": "custom_planned_completion_date" if t.get("custom_planned_completion_date") else "exp_end_date",
		"impact": t.get("custom_impact"),
		"action_needed": t.get("custom_action_needed"),
		"critical": bool(t["is_milestone"]) or t["priority"] in HIGH_PRIORITIES,
	}


def _open_tasks(employee: str) -> list[dict[str, Any]]:
	return frappe.get_all(
		"Task",
		filters={"custom_employee": employee, "status": ["in", OPEN_TASK_STATUSES]},
		fields=[
			"name",
			"subject",
			"status",
			"priority",
			"is_milestone",
			"progress",
			"project",
			"exp_end_date",
			"custom_planned_completion_date",
			"custom_project_manager",
			"custom_task_owner",
			"custom_impact",
			"custom_action_needed",
		],
		limit=500,
	)


def _tasks_by_deadline(employee: str, f_date, t_date) -> dict[datetime.date, list[dict[str, Any]]]:
	out: dict[datetime.date, list[dict[str, Any]]] = {}
	for t in _open_tasks(employee):
		deadline = _task_deadline(t)
		if deadline and f_date <= deadline <= t_date:
			out.setdefault(deadline, []).append(_task_row(t, deadline))
	return out


def _work_dependencies(employee, f_date, t_date, lookahead_days) -> dict[str, Any]:
	tasks = _open_tasks(employee)
	today_date = getdate(today())
	soon_cutoff = add_days(t_date, max(cint(lookahead_days), 0))

	in_window, soon_after, overdue, undated = [], [], [], []
	for t in tasks:
		deadline = _task_deadline(t)
		row = _task_row(t, deadline)
		if deadline is None:
			undated.append(row)
		elif deadline < today_date or t["status"] == "Overdue":
			overdue.append(row)
		elif f_date <= deadline <= t_date:
			in_window.append(row)
		elif t_date < deadline <= soon_cutoff:
			soon_after.append(row)

	at_risk = in_window + soon_after
	milestones_at_risk = [r for r in at_risk if r["is_milestone"]]
	critical_at_risk = [r for r in at_risk if r["critical"]]

	projects = frappe.get_all(
		"Project",
		filters={"custom_employee": employee, "status": ["not in", CLOSED_PROJECT_STATUSES]},
		fields=[
			"name",
			"project_name",
			"status",
			"priority",
			"percent_complete",
			"expected_end_date",
			"custom_planned_completion_date",
			"custom_project_manager",
			"custom_current_phase",
			"custom_overdue_task_percent",
		],
	)
	project_rows = [
		{
			"project": p["name"],
			"project_name": p["project_name"],
			"status": p["status"],
			"priority": p["priority"],
			"percent_complete": flt(p["percent_complete"]),
			"expected_end_date": str(p["expected_end_date"]) if p["expected_end_date"] else None,
			"planned_completion_date": str(p["custom_planned_completion_date"])
			if p["custom_planned_completion_date"]
			else None,
			"project_manager": p["custom_project_manager"],
			"current_phase": p["custom_current_phase"],
			"overdue_task_percent": flt(p["custom_overdue_task_percent"]),
			"ends_inside_leave": bool(
				p["expected_end_date"] and f_date <= getdate(p["expected_end_date"]) <= soon_cutoff
			),
		}
		for p in projects
	]

	# ? BOTH LEVELS GET NOTIFIED: THE PROJECT'S MANAGER OWNS THE SIGN-OFF, BUT A TASK-LEVEL
	# ? MANAGER IS THE ONE WHOSE WORK ACTUALLY SLIPS.
	pm_emails = sorted(
		{
			e
			for e in ([r["project_manager"] for r in at_risk] + [p["project_manager"] for p in project_rows])
			if _is_email(e)
		}
	)

	# ? ONE SUGGESTED custom_project_details ROW PER AFFECTED PROJECT, SO THE PM CAN SIGN OFF
	# ? ON THAT ROW IN THE UI. approve_status IS DELIBERATELY LEFT OUT — prompt_hr's
	# ? validate_project_details_approval REFUSES ANY VALUE SET BY ANYONE BUT THAT PM.
	suggested: dict[str, dict[str, Any]] = {}
	for row in at_risk:
		if not row["project"]:
			continue
		entry = suggested.setdefault(row["project"], {"project": row["project"], "tasks": []})
		entry["tasks"].append(f"{row['task']} {row['subject']} (due {row['deadline']}, {row['status']})")
	for p in project_rows:
		if p["ends_inside_leave"] and p["project"] not in suggested:
			suggested[p["project"]] = {
				"project": p["project"],
				"tasks": [f"Project end date {p['expected_end_date']} falls in or just after the leave"],
			}

	# ? Project Details.project_manager IS read_only WITH fetch_from=project.custom_project_manager,
	# ? SO THE PROJECT'S MANAGER — NOT THE TASK'S — IS WHAT THE ROW WILL END UP HOLDING, AND IS
	# ? THEREFORE WHO validate_project_details_approval WILL LET SIGN THAT ROW OFF. A TASK CAN
	# ? NAME A DIFFERENT PM; PUTTING THAT ONE IN THE ROW JUST GETS SILENTLY OVERWRITTEN ON SAVE.
	project_pms = {
		row["name"]: row["custom_project_manager"]
		for row in (
			frappe.get_all(
				"Project",
				filters={"name": ["in", list(suggested)]},
				fields=["name", "custom_project_manager"],
			)
			if suggested
			else []
		)
	}
	suggested_rows = [
		{
			"project": v["project"],
			"project_manager": project_pms.get(v["project"]),
			"task_description": "; ".join(v["tasks"])[:500],
		}
		for v in suggested.values()
	]

	return {
		"open_task_count": len(tasks),
		"tasks_due_in_window": sorted(in_window, key=lambda r: (r["deadline"] or "")),
		"tasks_due_within_days_after": sorted(soon_after, key=lambda r: (r["deadline"] or "")),
		"lookahead_days": cint(lookahead_days),
		"tasks_already_overdue": sorted(overdue, key=lambda r: (r["deadline"] or "")),
		"tasks_without_deadline": undated,
		"milestones_at_risk": milestones_at_risk,
		"critical_at_risk": critical_at_risk,
		"projects_owned": project_rows,
		"project_manager_emails": pm_emails,
		"suggested_project_details_rows": suggested_rows,
	}


# ==============================================================================
# TEAM COVERAGE
# ==============================================================================


def _team_coverage(emp, f_date, t_date, holiday_dates, exclude_leave_application) -> dict[str, Any]:
	"""Who else is out, over two units.

	The department here is large (dozens of people), so departmental coverage is almost
	always comfortable and tells a manager nothing. The unit that actually feels an
	absence is the immediate team — everyone reporting to the same manager, plus that
	manager. That is measured as `immediate_team`; the department stays as context.
	"""
	department = _coverage_for(
		emp,
		{"department": emp["department"], "status": "Active", "name": ["!=", emp["employee"]]}
		if emp["department"]
		else None,
		f_date,
		t_date,
		holiday_dates,
		exclude_leave_application,
		unit="department",
		unit_value=emp["department"],
	)

	immediate_filters = None
	if emp["reports_to"]:
		immediate_filters = {
			"reports_to": emp["reports_to"],
			"status": "Active",
			"name": ["not in", [emp["employee"], emp["reports_to"]]],
		}
	immediate = _coverage_for(
		emp,
		immediate_filters,
		f_date,
		t_date,
		holiday_dates,
		exclude_leave_application,
		unit="immediate_team",
		unit_value=emp["reports_to"],
		extra_members=[emp["reports_to"]] if emp["reports_to"] else None,
	)
	immediate["reports_to"] = emp["reports_to"]
	immediate["reports_to_name"] = emp["reports_to_name"]

	return {
		"immediate_team": immediate,
		"department": department,
		"primary_unit": "immediate_team" if immediate["team_size"] > 1 else "department",
	}


def _coverage_for(
	emp,
	peer_filters,
	f_date,
	t_date,
	holiday_dates,
	exclude_leave_application,
	*,
	unit,
	unit_value,
	extra_members=None,
) -> dict[str, Any]:
	if not peer_filters:
		return {
			"unit": unit,
			"unit_value": unit_value,
			"team_size": 0,
			"note": (
				"No reporting manager is set on the Employee, so there is no immediate team to measure."
				if unit == "immediate_team"
				else "Employee has no department, so departmental coverage could not be assessed."
			),
			"per_date": [],
			"peers_on_leave": [],
			"min_available": None,
			"min_coverage_pct": None,
			"worst_dates": [],
		}

	peers = frappe.get_all("Employee", filters=peer_filters, fields=["name", "employee_name", "designation"])
	for extra in extra_members or []:
		if extra and extra not in [p["name"] for p in peers] and extra != emp["employee"]:
			row = frappe.db.get_value("Employee", extra, ["employee_name", "designation"], as_dict=True) or {}
			peers.append({"name": extra, "employee_name": row.get("employee_name"), "designation": row.get("designation")})
	peer_names = [p["name"] for p in peers]
	peer_label = {p["name"]: p["employee_name"] for p in peers}
	team_size = len(peers) + 1

	filters = {
		"employee": ["in", peer_names or [""]],
		"docstatus": ["<", 2],
		"workflow_state": ["in", BLOCKING_LEAVE_STATES],
		"from_date": ["<=", t_date],
		"to_date": [">=", f_date],
	}
	if exclude_leave_application:
		filters["name"] = ["!=", exclude_leave_application]

	peer_leaves = (
		frappe.get_all(
			"Leave Application",
			filters=filters,
			fields=["name", "employee", "leave_type", "from_date", "to_date", "workflow_state"],
		)
		if peer_names
		else []
	)

	per_date = []
	for d in _daterange(f_date, t_date):
		if d in holiday_dates:
			per_date.append({"date": str(d), "holiday": True, "peers_on_leave": [], "available": None, "coverage_pct": None})
			continue
		out = [
			{
				"employee": l["employee"],
				"employee_name": peer_label.get(l["employee"]),
				"leave_type": l["leave_type"],
				"state": l["workflow_state"],
				"leave_application": l["name"],
			}
			for l in peer_leaves
			if getdate(l["from_date"]) <= d <= getdate(l["to_date"])
		]
		# ? THE APPLICANT IS COUNTED OUT TOO — THAT IS THE WHOLE QUESTION BEING ASKED.
		available = team_size - len(out) - 1
		per_date.append(
			{
				"date": str(d),
				"holiday": False,
				"peers_on_leave": out,
				"available": available,
				"coverage_pct": round(available * 100.0 / team_size, 1) if team_size else None,
			}
		)

	working = [p for p in per_date if not p["holiday"]]
	min_available = min((p["available"] for p in working), default=None)
	worst = [p["date"] for p in working if p["available"] == min_available] if working else []

	return {
		"unit": unit,
		"unit_value": unit_value,
		"team_size": team_size,
		"members": [p["name"] for p in peers] + [emp["employee"]],
		"per_date": per_date,
		"peers_on_leave": _stringify_dates(peer_leaves),
		"min_available": min_available,
		"min_coverage_pct": round(min_available * 100.0 / team_size, 1)
		if (min_available is not None and team_size)
		else None,
		"worst_dates": worst,
	}


# ==============================================================================
# CHECKS
# ==============================================================================


def _add(bucket: list, code: str, message: str) -> None:
	bucket.append({"code": code, "message": message})


def _check_employee_state(emp, req, blockers, concerns, notes) -> None:
	if emp["status"] != "Active":
		_add(blockers, "employee_not_active", f"Employee status is {emp['status']}, not Active.")

	if emp["date_of_joining"] and getdate(req["from_date"]) < getdate(emp["date_of_joining"]):
		_add(
			blockers,
			"before_joining_date",
			f"Leave starts {req['from_date']} but the employee joined {emp['date_of_joining']}. "
			"prompt_hr's validate_joining_date will throw on save.",
		)

	if emp["relieving_date"] and getdate(req["to_date"]) > getdate(emp["relieving_date"]):
		# ? A RELIEVING DATE ON AN ACTIVE EMPLOYEE IS STALE DATA, NOT AN EXIT — SEVERAL
		# ? RECORDS ON THIS SITE CARRY ONE. ONLY TREAT IT AS BINDING ONCE THEY HAVE LEFT.
		if emp["status"] == "Active":
			_add(
				notes,
				"stale_relieving_date",
				f"Employee is Active but carries a relieving date of {emp['relieving_date']}, "
				f"before the leave ends {req['to_date']}. Treated as stale data, not an exit.",
			)
		else:
			_add(
				blockers,
				"after_relieving_date",
				f"Leave ends {req['to_date']}, past the relieving date {emp['relieving_date']}.",
			)

	if emp["probation_status"] == "In Probation":
		_add(
			concerns,
			"in_probation",
			"Employee is still In Probation"
			+ (f" until {emp['probation_end_date']}" if emp["probation_end_date"] else "")
			+ ". Confirm the leave policy applies before approving.",
		)

	if not emp["leave_approver"] and not emp["reports_to"]:
		_add(
			concerns,
			"no_approver",
			"Employee has neither a leave_approver nor a reports_to, so there is no human to escalate to.",
		)


def _check_leave_type_rules(req, emp, rules, balance, working_days, blockers, concerns, notes) -> None:
	if not rules["is_lwp"]:
		if not balance["allocation_found"]:
			_add(
				blockers,
				"no_leave_allocation",
				f"No submitted Leave Allocation covers {req['from_date']}..{req['to_date']} for "
				f"{rules['leave_type']}, so the balance is zero and submit will fail.",
			)
		elif not balance["sufficient"]:
			_add(
				blockers,
				"insufficient_balance",
				f"{balance['days_requested']} day(s) requested against a consumable balance of "
				f"{balance['consumable_now']}.",
			)

	if rules["max_continuous_days_allowed"] and req["total_leave_days"] > rules["max_continuous_days_allowed"]:
		_add(
			blockers,
			"exceeds_max_continuous_days",
			f"{rules['leave_type']} allows at most {rules['max_continuous_days_allowed']} continuous day(s); "
			f"{req['total_leave_days']} requested.",
		)

	if rules["attachment_required"] and not req.get("custom_attachment"):
		_add(
			blockers,
			"attachment_missing",
			f"{rules['leave_type']} requires an attachment and custom_attachment is empty — "
			"prompt_hr's before_save will throw.",
		)

	if rules["prior_days_required"] and req.get("posting_date"):
		notice = date_diff(req["from_date"], getdate(req["posting_date"]))
		if notice <= rules["prior_days_required"]:
			_add(
				blockers,
				"insufficient_prior_notice",
				f"{rules['leave_type']} must be applied for at least {rules['prior_days_required']} day(s) "
				f"in advance; this was filed {notice} day(s) before.",
			)

	if emp["is_on_notice_period"] and not rules["allowed_during_notice_period"]:
		_add(
			blockers,
			"notice_period",
			f"{rules['leave_type']} cannot be applied during notice period "
			f"(resignation letter dated {emp['resignation_letter_date']}).",
		)

	if rules["is_festival_leave"] and req.get("half_day"):
		_add(blockers, "half_day_festival_leave", "Half Day Leave is not allowed for Festival Leave.")

	if rules["counts_holidays_as_leave"] and working_days < req["total_leave_days"]:
		_add(
			notes,
			"holidays_counted",
			f"{rules['leave_type']} counts holidays as leave, so all {req['total_leave_days']} day(s) are "
			f"deducted even though only {working_days} are working days.",
		)


def _check_dates(req, emp, rules, blackout, blockers, concerns) -> None:
	allowed_backdated = cint(
		frappe.db.get_single_value("HR Settings", "custom_maximum_backdated_leave_days_including_today")
	)
	if allowed_backdated and getdate(req["from_date"]) < getdate(add_days(today(), -(allowed_backdated - 1))):
		_add(
			blockers,
			"too_far_backdated",
			f"Leave starts {req['from_date']}, beyond the {allowed_backdated}-day backdated window "
			"HR Settings allows.",
		)

	if blackout["blocked_dates"]:
		listed = ", ".join(f"{b['date']} ({b['reason']})" for b in blackout["blocked_dates"])
		_add(blockers, "leave_block_list", f"Blocked date(s) inside the window: {listed}.")

	# ? A REQUEST FILED FOR TOMORROW GIVES THE TEAM NO TIME TO REPLAN.
	if req.get("posting_date"):
		notice = date_diff(req["from_date"], getdate(req["posting_date"]))
		if 0 <= notice <= 1 and req["total_leave_days"] > 1:
			_add(
				concerns,
				"short_notice",
				f"{req['total_leave_days']} day(s) requested with {notice} day(s) of notice.",
			)


def _check_own_conflicts(own, blockers, concerns) -> None:
	if own["overlapping_leave_applications"]:
		names = ", ".join(
			f"{l['name']} ({l['workflow_state']}, {l['from_date']}..{l['to_date']})"
			for l in own["overlapping_leave_applications"]
		)
		_add(blockers, "overlapping_leave", f"Overlapping Leave Application(s) already on file: {names}.")

	submitted = [a for a in own["attendance_already_marked"] if a["docstatus"] == 1]
	if submitted:
		dates = ", ".join(f"{a['attendance_date']} ({a['status']})" for a in submitted)
		_add(
			concerns,
			"attendance_already_marked",
			f"Attendance is already submitted for {dates}; approving leave will clash with it.",
		)

	if own["overlapping_attendance_requests"]:
		names = ", ".join(
			f"{r['name']} ({r['reason']}, {r['workflow_state']})" for r in own["overlapping_attendance_requests"]
		)
		_add(concerns, "attendance_request_overlap", f"Overlapping Attendance Request(s): {names}.")


def _check_work(work, concerns, notes) -> None:
	for row in work["milestones_at_risk"]:
		_add(
			concerns,
			"milestone_at_risk",
			f"Milestone {row['task']} \"{row['subject']}\" on {row['project']} is due {row['deadline']} "
			f"({row['status']}, {row['progress']}% done).",
		)

	high = [r for r in work["critical_at_risk"] if not r["is_milestone"]]
	for row in high[:10]:
		_add(
			concerns,
			"high_priority_task_at_risk",
			f"{row['priority']}-priority task {row['task']} \"{row['subject']}\" is due {row['deadline']} "
			f"({row['status']}, {row['progress']}% done).",
		)

	other = [
		r
		for r in work["tasks_due_in_window"] + work["tasks_due_within_days_after"]
		if not r["critical"]
	]
	if other:
		_add(
			concerns,
			"tasks_due_in_window",
			f"{len(other)} further task(s) fall due inside the leave or within "
			f"{work['lookahead_days']} day(s) of the return: "
			+ ", ".join(f"{r['task']} (due {r['deadline']})" for r in other[:8])
			+ ("…" if len(other) > 8 else ""),
		)

	# ? ALREADY-OVERDUE WORK IS NOT A REASON TO REFUSE LEAVE, AND ON THIS SITE ALMOST
	# ? EVERYONE HAS SOME, SO IT IS A NOTE RATHER THAN A CONFLICT.
	if work["tasks_already_overdue"]:
		_add(
			notes,
			"backlog",
			f"{len(work['tasks_already_overdue'])} task(s) assigned to this employee are already overdue, "
			"independently of this leave.",
		)

	for p in work["projects_owned"]:
		if p["ends_inside_leave"]:
			_add(
				concerns,
				"project_deadline_in_window",
				f"Project {p['project']} \"{p['project_name']}\" is due {p['expected_end_date']} "
				f"({p['percent_complete']}% complete, phase {p['current_phase']}).",
			)


def _check_coverage(coverage, concerns) -> None:
	unit = coverage[coverage["primary_unit"]]
	label = (
		f"the team reporting to {unit.get('reports_to_name') or unit['unit_value']}"
		if unit["unit"] == "immediate_team"
		else unit["unit_value"]
	)
	if not unit["team_size"] or unit["min_available"] is None:
		return
	if unit["min_available"] <= 0:
		_add(
			concerns,
			"no_coverage",
			f"On {', '.join(unit['worst_dates'])} nobody in {label} would be available.",
		)
	elif unit["min_coverage_pct"] is not None and unit["min_coverage_pct"] < 50:
		_add(
			concerns,
			"thin_coverage",
			f"{label} drops to {unit['min_available']} of {unit['team_size']} available "
			f"({unit['min_coverage_pct']}%) on {', '.join(unit['worst_dates'])}.",
		)
	peers_out = {p["employee"] for d in unit["per_date"] for p in d.get("peers_on_leave", [])}
	if peers_out:
		names = ", ".join(sorted(peers_out))
		_add(
			concerns,
			"peers_already_out",
			f"Already on leave in {label} over the same dates: {names}.",
		)


def _recommendation(verdict, blockers, concerns, req) -> str:
	if verdict == "clean":
		return (
			"APPROVE. No blocker and no conflict found — set status='Approved', then run_action "
			"'Approve', then tell the employee. Anything under `notes` is background only and is "
			"not a reason to hold the request."
		)
	if verdict == "blocked":
		return (
			f"DO NOT APPROVE. {len(blockers)} blocker(s) mean the save or submit would be refused by "
			"this install's own validations. Leave the application Pending, and report every blocker "
			"message verbatim to the employee and the reporting manager."
		)
	return (
		f"ESCALATE. {len(concerns)} conflict(s) found — nothing is invalid, but a human owns this call. "
		"Leave the application Pending, add the suggested project rows so each project manager can "
		"sign off on their own row, and send the impact summary to the reporting manager, the leave "
		"approver and the affected project managers."
	)


# ==============================================================================
# SMALL HELPERS
# ==============================================================================


def _notify_targets(emp, req, work) -> dict[str, Any]:
	return {
		"employee_email": emp["email"],
		"reporting_manager": emp["reports_to"],
		"reporting_manager_name": emp["reports_to_name"],
		"reporting_manager_email": emp["reports_to_email"],
		"leave_approver": req.get("leave_approver") or emp["leave_approver"],
		"project_manager_emails": work.get("project_manager_emails", []),
		"note": (
			"This site's outgoing mail queue is backed up — report anything you send as QUEUED, "
			"never as delivered."
		),
	}


def _first_email(*candidates) -> str | None:
	for c in candidates:
		if _is_email(c):
			return c
	return None


def _is_email(value) -> bool:
	return bool(value) and isinstance(value, str) and "@" in value


def _compact(row, keys) -> dict[str, Any] | None:
	if not row:
		return None
	return {k: (str(row[k]) if isinstance(row.get(k), (datetime.date, datetime.datetime, datetime.timedelta)) else row.get(k)) for k in keys if k in row}


def _stringify_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	out = []
	for row in rows:
		out.append(
			{
				k: (str(v) if isinstance(v, (datetime.date, datetime.datetime, datetime.timedelta)) else v)
				for k, v in row.items()
			}
		)
	return out
