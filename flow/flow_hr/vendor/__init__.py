# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Is the other-app code these agents call actually patched on this site?

The agents reach into prompt_hr, and a couple of those call paths are broken on hrms 16 as
shipped -- see README.md and the patch beside it. A broken one does not fail here, it fails
inside an agent run, as a stack trace in a Flow Run that reads like the agent's fault.

`check()` looks for the symptoms rather than the patch: whether the import that moved in
hrms 16 resolves, and whether the function a commented-out `def` used to swallow is present.
"""

from __future__ import annotations

import frappe

# Relative to apps/prompt_hr, which is where it has to be applied from.
FIX = "cd apps/prompt_hr && git apply ../flow/flow/flow_hr/vendor/prompt_hr-hrms16-compat.patch"


def check(verbose: bool = True) -> dict:
	"""Report which of the fixes this pack depends on are present. Never raises."""
	results = [_hrms_payroll_utils(), _job_offer_imports(), _standard_salary_imports(), _fnf_hooks()]
	report = {
		"ok": all(r["ok"] for r in results),
		"checks": results,
	}
	if not report["ok"]:
		report["fix"] = FIX

	if verbose:
		print(frappe.as_json(report, indent=2))
	return report


def _result(name: str, ok: bool, detail: str, needed_by: str) -> dict:
	return {"check": name, "ok": ok, "detail": detail, "needed_by": needed_by}


def _hrms_payroll_utils() -> dict:
	"""The v16 home of the salary-component eval helpers. Nothing else can be right without it."""
	try:
		from hrms.payroll.utils import COMPONENT_EVAL_GLOBALS, get_component_abbr_map
	except ImportError as e:
		return _result("hrms.payroll.utils", False, f"{e}", "Payroll Agent, Onboarding Agent")
	return _result("hrms.payroll.utils", True, "importable", "Payroll Agent, Onboarding Agent")


def _job_offer_imports() -> dict:
	try:
		import prompt_hr.py.job_offer
	except ImportError as e:
		return _result(
			"prompt_hr.py.job_offer",
			False,
			f"module will not import -- Job Offer salary annexures fail: {e}",
			"Onboarding Agent",
		)
	return _result("prompt_hr.py.job_offer", True, "imports cleanly", "Onboarding Agent")


def _standard_salary_imports() -> dict:
	try:
		import prompt_hr.prompt_hr.doctype.employee_standard_salary.employee_standard_salary
	except ImportError as e:
		return _result(
			"Employee Standard Salary",
			False,
			f"module will not import -- salary previews fail: {e}",
			"Payroll Agent",
		)
	return _result("Employee Standard Salary", True, "imports cleanly", "Payroll Agent")


def _fnf_hooks() -> dict:
	"""`before_insert` must not be hooked: its handler is commented out in the module, and a
	commented-out `def` there once swallowed the body of the function after it."""
	hooked = frappe.get_hooks("doc_events").get("Full and Final Statement", {}).get("before_insert")
	if hooked:
		return _result(
			"Full and Final Statement hooks",
			False,
			f"before_insert is still hooked to {hooked} -- its handler is commented out",
			"Offboarding Agent",
		)
	return _result("Full and Final Statement hooks", True, "before_insert not hooked", "Offboarding Agent")
