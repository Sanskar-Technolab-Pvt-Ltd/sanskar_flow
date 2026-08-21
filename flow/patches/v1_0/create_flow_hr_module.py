"""Create the Flow HR Module Def before the DocType sync needs it.

`bench migrate` never creates Module Defs -- only `bench install-app` does, through
`frappe.installer.add_module_defs`. So on a site that already has flow installed, the two
doctypes moving into the new `Flow HR` module would fail link validation on `module` during
the schema sync. Hence pre_model_sync.
"""

import frappe

MODULE = "Flow HR"


def execute():
	if frappe.db.exists("Module Def", MODULE):
		frappe.db.set_value("Module Def", MODULE, "app_name", "flow")
		return

	frappe.get_doc(
		{
			"doctype": "Module Def",
			"module_name": MODULE,
			"app_name": "flow",
		}
	).insert(ignore_permissions=True)
