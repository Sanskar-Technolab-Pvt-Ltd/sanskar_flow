
// ? ============================================================================
// ? EXIT TAB: START OFFBOARDING
// ? ---------------------------------------------------------------------------
// ? The Offboarding Agent no longer waits for an Employee Separation to be created.
// ? HR clicks this button on the Exit tab, sets the relieving date in the dialog, and the
// ? agent then runs with that date already saved -- which is what lets it create the Exit
// ? Interview and the Full and Final Statement (hrms refuses both while the relieving date
// ? is blank) instead of skipping them and asking a human to come back later.
// ? ============================================================================

frappe.ui.form.on("Employee", {
    refresh(frm) {
        toggleStartOffboardingButton(frm);
    },

    custom_start_offboarding(frm) {
        startOffboardingDialog(frm);
    }
});

// ? HIDE THE BUTTON WHERE IT CANNOT DO ANYTHING: UNSAVED DOC, OR ALREADY LEFT
function toggleStartOffboardingButton(frm) {
    const usable = !frm.is_new() && frm.doc.status !== "Left";
    frm.set_df_property("custom_start_offboarding", "hidden", usable ? 0 : 1);
}

function startOffboardingDialog(frm) {
    if (frm.is_dirty()) {
        frappe.msgprint({
            title: __("Unsaved Changes"),
            message: __("Please save the Employee before starting offboarding."),
            indicator: "orange"
        });
        return;
    }

    frappe.call({
        method: "flow.flow_hr.offboarding.get_offboarding_state",
        args: { employee: frm.doc.name },
        freeze: true,
        freeze_message: __("Checking exit records..."),
        callback(r) {
            if (r.exc || !r.message) return;
            const state = r.message;

            if (!state.allowed) {
                frappe.msgprint({
                    title: __("Not Permitted"),
                    message: __("Only HR can start the offboarding process."),
                    indicator: "red"
                });
                return;
            }

            showOffboardingDialog(frm, state);
        }
    });
}

function showOffboardingDialog(frm, state) {
    // ? WHAT ALREADY EXISTS -- SO A SECOND CLICK READS AS A RE-RUN, NOT A DUPLICATE
    const existing = [
        state.separation && [__("Employee Separation"), "Employee Separation", state.separation.name],
        state.it_exit_checklist && [__("IT Exit Checklist"), "IT Exit Checklist", state.it_exit_checklist],
        state.exit_interview && [__("Exit Interview"), "Exit Interview", state.exit_interview],
        state.full_and_final && [__("Full and Final Statement"), "Full and Final Statement", state.full_and_final]
    ].filter(Boolean);

    let intro = __("The relieving date is saved on this Employee first, then the Offboarding Agent creates the Employee Separation, the IT Exit Checklist, the Exit Interview and the Full and Final Statement, and emails the employee.");

    if (existing.length) {
        const links = existing
            .map(([label, dt, dn]) => `<li>${label}: <a href="/app/${frappe.router.slug(dt)}/${encodeURIComponent(dn)}">${frappe.utils.escape_html(dn)}</a></li>`)
            .join("");
        intro += `<br><br><b>${__("Already created for this employee")}:</b><ul>${links}</ul>${__("The agent links to these instead of creating duplicates.")}`;
    }

    if (!state.has_email) {
        intro += `<br><br><span style="color:var(--orange-600)">${__("This employee has no personal or company email, so the offboarding email will be skipped.")}</span>`;
    }

    const dialog = new frappe.ui.Dialog({
        title: __("Start Offboarding"),
        fields: [
            { fieldtype: "HTML", options: `<p class="text-muted small">${intro}</p>` },
            {
                fieldname: "relieving_date",
                label: __("Relieving Date"),
                fieldtype: "Date",
                reqd: 1,
                default: state.relieving_date || frappe.datetime.get_today(),
                description: __("Last working day. Everything downstream is blocked without it.")
            },
            {
                fieldname: "resignation_letter_date",
                label: __("Resignation Letter Date"),
                fieldtype: "Date",
                default: state.resignation_letter_date || frappe.datetime.get_today()
            },
            { fieldtype: "Column Break" },
            {
                fieldname: "reason_for_leaving",
                label: __("Reason for Leaving"),
                fieldtype: "Small Text"
            },
            {
                fieldname: "notice_period_served",
                label: __("Is Notice Period Served?"),
                fieldtype: "Check",
                default: frm.doc.custom_is_notice_period_served ? 1 : 0
            }
        ],
        primary_action_label: __("Set Date & Run Agent"),
        primary_action(values) {
            if (state.date_of_joining && values.relieving_date < state.date_of_joining) {
                frappe.msgprint({
                    title: __("Invalid Relieving Date"),
                    message: __("The relieving date cannot be before the date of joining ({0}).", [
                        frappe.datetime.str_to_user(state.date_of_joining)
                    ]),
                    indicator: "red"
                });
                return;
            }

            frappe.call({
                method: "flow.flow_hr.offboarding.start_offboarding",
                args: {
                    employee: frm.doc.name,
                    relieving_date: values.relieving_date,
                    resignation_letter_date: values.resignation_letter_date,
                    reason_for_leaving: values.reason_for_leaving,
                    notice_period_served: values.notice_period_served ? 1 : 0
                },
                freeze: true,
                freeze_message: __("Setting relieving date and starting the Offboarding Agent..."),
                callback(r) {
                    if (r.exc || !r.message) return;
                    dialog.hide();
                    frm.reload_doc();
                    frappe.msgprint({
                        title: __("Offboarding Started"),
                        message: __(
                            "Relieving date set to <b>{0}</b>. The <b>{1}</b> is now running in the background — the exit records appear on this employee within a couple of minutes.<br><br>Its progress and what it created are on the <a href='/app/flow-run?reference_name={2}'>Flow Run</a> list.",
                            [
                                frappe.datetime.str_to_user(r.message.relieving_date),
                                r.message.agent || __("Offboarding Agent"),
                                encodeURIComponent(frm.doc.name)
                            ]
                        ),
                        indicator: "green"
                    });
                }
            });
        }
    });

    dialog.show();
}
