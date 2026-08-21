# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Flow tools for payroll.

Registered as Flow Tool rows of type "Imported" (see `preview_salary_slip` below).
The Payroll Agent's instructions promise a `preview_salary_slip` tool; this is it.
Everything here is read-only — nothing is inserted, so no Salary Slip record is
created and no docstatus is ever changed.
"""

from __future__ import annotations

from typing import Annotated, Any

import frappe
from frappe import _
from frappe.utils import flt, getdate, today


def preview_salary_slip(
	employee: Annotated[str, "Employee ID, e.g. ST-EMP-00045. Never the person's name."],
	posting_date: Annotated[
		str | None,
		"Any date inside the month to compute, as YYYY-MM-DD. Defaults to today.",
	] = None,
	salary_structure: Annotated[
		str | None,
		"Salary Structure to compute against. Defaults to the one on the employee's "
		"submitted Salary Structure Assignment effective on posting_date.",
	] = None,
) -> dict[str, Any]:
	"""Compute an employee's salary for one month WITHOUT creating any record.

	Runs HRMS's own payroll engine in preview mode and reads back what it produced:
	every earning and deduction, gross pay, total deduction and net pay, plus the
	working/payment days the figures were prorated on.

	Nothing is saved — this creates no Salary Slip and changes no data. Report the
	returned figures exactly as they are; never round or recompute them.

	Always check `tax_scaffolding_complete` before presenting any tax figure. When it is
	false, `tax_scaffolding_issues` lists what is missing and the income tax number is
	not trustworthy. When it is true, an income tax of 0 is a real result — see
	`income_tax_note` for why.
	"""
	from hrms.payroll.doctype.salary_structure.salary_structure import make_salary_slip

	frappe.has_permission("Salary Slip", "read", throw=True)

	posting_date = getdate(posting_date or today())
	assignment = _get_assignment(employee, posting_date)
	salary_structure = salary_structure or assignment.salary_structure

	slip = make_salary_slip(
		salary_structure,
		employee=employee,
		posting_date=posting_date,
		for_preview=1,
	)

	scaffolding_issues = _tax_scaffolding_issues(slip, assignment)

	return {
		"employee": slip.employee,
		"employee_name": slip.employee_name
		or frappe.db.get_value("Employee", slip.employee, "employee_name"),
		"company": slip.company,
		"salary_structure": slip.salary_structure,
		"salary_structure_assignment": assignment.name,
		"start_date": str(slip.start_date),
		"end_date": str(slip.end_date),
		"posting_date": str(slip.posting_date),
		"currency": slip.currency,
		"total_working_days": flt(slip.total_working_days),
		"payment_days": flt(slip.payment_days),
		"leave_without_pay": flt(slip.leave_without_pay),
		"absent_days": flt(slip.absent_days),
		"earnings": _rows(slip.get("earnings")),
		"deductions": _rows(slip.get("deductions")),
		"gross_pay": flt(slip.gross_pay),
		"total_deduction": flt(slip.total_deduction),
		"net_pay": flt(slip.net_pay),
		"rounded_total": flt(slip.rounded_total),
		"income_tax_slab": assignment.income_tax_slab,
		"tax_scaffolding_complete": not scaffolding_issues,
		"tax_scaffolding_issues": scaffolding_issues,
		"income_tax_note": _income_tax_note(slip, assignment, scaffolding_issues),
		"is_preview": True,
		"record_created": None,
	}


def _rows(rows: list[Any] | None) -> list[dict[str, Any]]:
	return [
		{
			"salary_component": row.salary_component,
			"amount": flt(row.amount),
			"default_amount": flt(row.default_amount),
		}
		for row in (rows or [])
	]


def _get_assignment(employee: str, posting_date: Any) -> Any:
	"""The submitted Salary Structure Assignment in force on posting_date."""
	assignment = frappe.db.get_value(
		"Salary Structure Assignment",
		{
			"employee": employee,
			"docstatus": 1,
			"from_date": ["<=", posting_date],
		},
		["name", "salary_structure", "income_tax_slab", "base", "from_date"],
		order_by="from_date desc",
		as_dict=True,
	)
	if not assignment:
		frappe.throw(
			_(
				"No submitted Salary Structure Assignment for {0} effective on {1}. "
				"Payroll cannot be computed until HR creates and submits one — do not guess a structure or a base."
			).format(employee, posting_date),
			title=_("No Salary Structure Assignment"),
		)
	return assignment


def _tax_scaffolding_issues(slip: Any, assignment: Any) -> list[str]:
	"""What is missing before the computed income tax can be believed."""
	issues = []

	if not getattr(slip, "payroll_period", None):
		issues.append(
			f"No Payroll Period covers {slip.start_date} for {slip.company}, "
			"so annual tax cannot be projected."
		)

	if not frappe.db.exists(
		"Salary Component", {"variable_based_on_taxable_salary": 1, "disabled": 0}
	):
		issues.append(
			"No enabled Salary Component is marked 'Variable Based On Taxable Salary', "
			"so no income tax component is applied at all."
		)

	if not assignment.income_tax_slab:
		issues.append(
			f"Salary Structure Assignment {assignment.name} has no Income Tax Slab linked, "
			"so no regime (old/new) applies."
		)

	return issues


def _income_tax_note(slip: Any, assignment: Any, scaffolding_issues: list[str]) -> str | None:
	"""Explain a zero income tax so it is not mistaken for a silent gap."""
	tax = sum(
		flt(row.amount)
		for row in (slip.get("deductions") or [])
		if frappe.get_cached_value("Salary Component", row.salary_component, "variable_based_on_taxable_salary")
	)
	if tax or scaffolding_issues or not assignment.income_tax_slab:
		return None

	relief_limit = frappe.db.get_value("Income Tax Slab", assignment.income_tax_slab, "tax_relief_limit")
	projected = flt(getattr(slip, "total_taxable_earnings", 0))
	if relief_limit and projected and projected <= flt(relief_limit):
		return (
			f"Income tax is 0 because the projected annual taxable earning of {projected:,.2f} "
			f"is at or below the {assignment.income_tax_slab} tax relief limit of {flt(relief_limit):,.2f}. "
			"This is a computed result, not missing configuration."
		)
	return None
