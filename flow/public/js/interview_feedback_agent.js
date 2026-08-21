// ============================================================================
// INTERVIEW FEEDBACK AGENT
// ============================================================================
// The transcript does not exist when the Interview is saved -- it arrives later, from
// whatever recorded the call -- so the agent hangs off a button (a Manual Flow Trigger)
// rather than a doc event. This block only reads state and fires the trigger; where a
// transcript is looked for is configured in Interview Feedback Agent Settings.

frappe.ui.form.on("Interview", {
    refresh: function (frm) {
        if (frm.is_new() || frm.doc.docstatus === 2) return;
        addFeedbackAgentButton(frm);
    },
});

function addFeedbackAgentButton(frm) {
    frappe.call({
        method: "flow.flow_hr.interview_feedback_agent.get_transcript_state",
        args: { interview: frm.doc.name },
        callback: function (r) {
            const state = r.message;
            if (!state || !state.enabled || !state.allowed) return;

            const found = !!state.transcript;
            const label = found
                ? __("Draft Feedback from Transcript")
                : __("Transcript Not Found");

            frm.add_custom_button(label, () => {
                if (!found) {
                    showTranscriptAttempts(state);
                    return;
                }
                confirmFeedbackRun(frm, state);
            }, __("Actions"));
        },
    });
}

// ? WHY NOTHING WAS FOUND, IN THE WORDS OF EACH SOURCE THAT WAS TRIED. Without this the
// ? recruiter is left guessing whether the transcript is missing or the config is wrong.
function showTranscriptAttempts(state) {
    const rows = (state.attempts || []).map(
        (a) => `<tr><td>${frappe.utils.escape_html(a.source || "")}</td>
                    <td>${frappe.utils.escape_html(a.type || "")}</td>
                    <td>${frappe.utils.escape_html(a.status || "")}</td>
                    <td class="text-muted">${frappe.utils.escape_html(a.detail || "")}</td></tr>`
    );

    const body = rows.length
        ? `<p>${__("No transcript could be read for this interview. Each configured source was tried:")}</p>
           <table class="table table-bordered small">
             <thead><tr><th>${__("Source")}</th><th>${__("Type")}</th><th>${__("Result")}</th><th>${__("Detail")}</th></tr></thead>
             <tbody>${rows.join("")}</tbody>
           </table>
           <p class="text-muted">${__("File the transcript on this Interview, or add a source for the tool that recorded it in Interview Feedback Agent Settings.")}</p>`
        : `<p>${__("No transcript sources are configured. Add at least one row to Interview Feedback Agent Settings &rarr; Sources.")}</p>`;

    frappe.msgprint({ title: __("Transcript Not Found"), message: body, indicator: "orange" });
}

function confirmFeedbackRun(frm, state) {
    const t = state.transcript;
    const speakers = (t.speakers || [])
        .map((s) => `${frappe.utils.escape_html(s.label)}${s.role ? ` <b>[${s.role}]</b>` : ` <span class="text-muted">[${__("role unknown")}]</span>`} &mdash; ${s.turns} ${__("turns")}`)
        .join("<br>");

    const warnings = (t.warnings || []).length
        ? `<div class="alert alert-warning small">${t.warnings.map((w) => frappe.utils.escape_html(w)).join("<br>")}</div>`
        : "";

    const target =
        state.write_mode === "Fill Interviewer Draft"
            ? __("the interviewers' draft Interview Feedback (left unsubmitted)")
            : __("a comment on this Interview (no record is changed)");

    frappe.confirm(
        `<p>${__("Found a transcript for this interview:")}</p>
         <ul>
           <li>${__("Source")}: <b>${frappe.utils.escape_html(t.source || "")}</b> (${frappe.utils.escape_html(t.format || "")})</li>
           <li>${__("Length")}: ${t.chars} ${__("characters")}, ${t.turns} ${__("turns")}</li>
           <li>${__("Rubric")}: ${(state.rubric_skills || []).map((s) => frappe.utils.escape_html(s)).join(", ") || __("nothing configured on this Interview Type")}</li>
         </ul>
         <p>${speakers}</p>
         ${warnings}
         <p>${__("The assessment will be written to")} ${target}. ${__("Nothing is submitted -- the interviewer confirms their own verdict.")}</p>`,
        () => {
            frappe.call({
                method: "flow.flow_hr.interview_feedback_agent.generate_feedback_from_transcript",
                args: { interview: frm.doc.name },
                freeze: true,
                freeze_message: __("Handing the transcript to the Interview Feedback Agent..."),
                callback: function (r) {
                    if (!r.message) return;
                    frappe.msgprint({
                        title: __("Agent Started"),
                        message: `${__("The Interview Feedback Agent is reading the transcript. Its draft will appear on this Interview shortly; the run is visible under Flow Run.")}`,
                        indicator: "green",
                    });
                },
            });
        }
    );
}
