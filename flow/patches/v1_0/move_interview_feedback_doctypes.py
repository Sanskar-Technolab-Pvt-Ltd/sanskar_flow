"""Reassign the Interview Feedback Agent's config doctypes from prompt_hr to flow.

They were authored in prompt_hr and have moved here with the rest of the agent pack, so the
`Flow HR` module now owns them. The schema sync normally does this itself when it imports the
new JSON, but it compares timestamps and skips a file that is not newer than the row -- so
this is the belt for a site where that comparison went the wrong way.

Only the `module` field changes. The tables, the rows in them, and every configured transcript
source stay exactly where they are.
"""

import frappe

MODULE = "Flow HR"
DOCTYPES = ("Interview Feedback Agent Settings", "Interview Transcript Source")


def execute():
	if not frappe.db.exists("Module Def", MODULE):
		# create_flow_hr_module runs first; if it did not, there is nothing safe to point at.
		return

	for doctype in DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		if frappe.db.get_value("DocType", doctype, "module") == MODULE:
			continue
		frappe.db.set_value("DocType", doctype, "module", MODULE, update_modified=False)

	frappe.clear_cache()
