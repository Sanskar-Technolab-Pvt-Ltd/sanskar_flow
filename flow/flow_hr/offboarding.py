"""Offboarding kickoff from the Employee form's Exit tab.

The Offboarding Agent used to be wired to `Employee Separation / after_insert`, which meant
HR had to hand-create the separation first, and the agent then found the Employee's
relieving_date still blank -- so it always skipped the Exit Interview and the Full and Final
Statement (hrms refuses to create either without one) and ended with "a human must set the
relieving date".

This module inverts that: HR clicks "Start Offboarding" on the Exit tab, sets the relieving
date in one dialog, and only then does the agent run -- with the date already in place, so it
can create the Employee Separation, the Exit Interview and the F&F Statement in one pass.
"""

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

# The Manual Flow Trigger this button fires. Manual triggers are dispatched by nothing --
# `flow.triggers.fire_manual` is the only thing that ever runs them.
OFFBOARDING_TRIGGER = "Offboarding Agent - Exit Button"

# Same gate the Raise Termination button uses in employee.js -- offboarding is an HR action.
HR_ROLES = (
    "S - HR Leave Approval",
    "S - HR leave Report",
    "S - HR L6",
    "S - HR L5",
    "S - HR L4",
    "S - HR L3",
    "S - HR L2",
    "S - HR L1",
    "S - HR Director (Global Admin)",
    "S - HR L2 Manager",
    "S - HR Supervisor (RM)",
    "System Manager",
)


def _can_offboard(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return bool(set(frappe.get_roles(user)) & set(HR_ROLES))


@frappe.whitelist()
def get_offboarding_state(employee):
    """What the Exit tab needs to render the button: whether the click is allowed, and what
    has already been created for this employee so a second click reads as a re-run."""
    emp = frappe.db.get_value(
        "Employee",
        employee,
        [
            "name",
            "employee_name",
            "status",
            "company",
            "date_of_joining",
            "relieving_date",
            "resignation_letter_date",
            "personal_email",
            "company_email",
        ],
        as_dict=True,
    )
    if not emp:
        frappe.throw(_("Employee {0} not found.").format(employee))

    separation = frappe.db.get_value(
        "Employee Separation",
        {"employee": employee, "docstatus": ["<", 2]},
        ["name", "docstatus", "boarding_status"],
        as_dict=True,
    )

    return {
        "allowed": _can_offboard(),
        "employee_name": emp.employee_name,
        "status": emp.status,
        "date_of_joining": emp.date_of_joining,
        "relieving_date": emp.relieving_date,
        "resignation_letter_date": emp.resignation_letter_date,
        "has_email": bool(emp.personal_email or emp.company_email),
        "separation": separation,
        "exit_interview": frappe.db.get_value("Exit Interview", {"employee": employee}, "name"),
        "full_and_final": frappe.db.get_value(
            "Full and Final Statement", {"employee": employee, "docstatus": ["<", 2]}, "name"
        ),
        "it_exit_checklist": frappe.db.get_value(
            "IT Exit Checklist", {"employee": employee}, "name"
        ),
    }


@frappe.whitelist()
def start_offboarding(
    employee,
    relieving_date,
    resignation_letter_date=None,
    reason_for_leaving=None,
    notice_period_served=None,
):
    """Set the relieving date on the Employee, then fire the Offboarding Agent.

    The order matters: the agent reads relieving_date on its first tool call, so the date has
    to be committed to the Employee record before the run starts. `fire_manual` enqueues with
    `enqueue_after_commit=True`, so the worker only picks the job up once this request's
    transaction -- including the save below -- has landed.
    """
    if not _can_offboard():
        frappe.throw(
            _("Only HR roles can start offboarding."), frappe.PermissionError
        )
    frappe.has_permission("Employee", "write", doc=employee, throw=True)

    if not relieving_date:
        frappe.throw(_("Relieving Date is required to start offboarding."))

    doc = frappe.get_doc("Employee", employee)

    if doc.status == "Left":
        frappe.throw(
            _("{0} is already marked Left. Offboarding has nothing left to start.").format(
                doc.employee_name
            )
        )
    if doc.date_of_joining and getdate(relieving_date) < getdate(doc.date_of_joining):
        frappe.throw(
            _("Relieving Date {0} cannot be before the Date of Joining {1}.").format(
                frappe.format(getdate(relieving_date), {"fieldtype": "Date"}),
                frappe.format(getdate(doc.date_of_joining), {"fieldtype": "Date"}),
            )
        )

    doc.relieving_date = getdate(relieving_date)
    # hrms reads resignation_letter_date onto the Employee Separation; default it to today so
    # the separation the agent creates is not missing it.
    doc.resignation_letter_date = getdate(
        resignation_letter_date or doc.resignation_letter_date or nowdate()
    )
    if reason_for_leaving:
        doc.reason_for_leaving = reason_for_leaving
    if notice_period_served is not None:
        doc.custom_is_notice_period_served = 1 if frappe.utils.cint(notice_period_served) else 0
    doc.save()

    from flow.triggers import fire_manual

    fire_manual(OFFBOARDING_TRIGGER, target_doctype="Employee", target_name=doc.name)

    return {
        "employee": doc.name,
        "employee_name": doc.employee_name,
        "relieving_date": str(doc.relieving_date),
        "agent": frappe.db.get_value("Flow Trigger", OFFBOARDING_TRIGGER, "agent"),
    }
