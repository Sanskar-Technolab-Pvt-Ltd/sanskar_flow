"""Install the HR agent pack on a site that already has flow.

A fresh `bench install-app flow` never reaches this patch: `frappe.installer.install_app`
calls `set_all_patches_as_completed()` before the `after_install` hooks, so the app's own
patches are marked done and skipped. The `after_install` hook covers that case; this covers
every site where flow is already installed and only `bench migrate` will run.

Both paths call the same idempotent `install()`, so it does not matter if both fire.
"""

import frappe


def execute():
	from flow.flow_hr.install import _hr_stack_present, install

	if not _hr_stack_present():
		# No hrms recruitment doctypes here, so there is nothing for these agents to act on
		# and a trigger pointing at a missing DocType would fail link validation.
		print(
			"flow: skipping the HR agent pack -- this site has no hrms recruitment doctypes.\n"
			"      After installing hrms, run:\n"
			"        bench --site <site> execute flow.flow_hr.install.install"
		)
		return

	try:
		install()
	except Exception:
		# A patch that raises aborts the whole migrate. The pack is additive configuration --
		# it must never be the reason a site cannot be upgraded.
		frappe.log_error(title="Flow HR agent pack install failed")
		print(
			"flow: the HR agent pack did not install cleanly -- see the Error Log.\n"
			"      Re-run it with: bench --site <site> execute flow.flow_hr.install.install"
		)
