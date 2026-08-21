# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

"""Entry points for the Interview Feedback Agent.

The agent reads the interview transcript and drafts the feedback. Nothing about *when* it
runs can be a doc event, because a transcript does not arrive when the Interview is saved --
it arrives minutes or hours later, from whatever recorded the call, and often from outside
Frappe altogether. So there are exactly two ways in, and both end at the same Manual Flow
Trigger:

  * `generate_feedback_from_transcript` -- the button on the Interview's Transcript section,
    for a recruiter who can see the transcript is there and wants the draft now.
  * `ingest_transcript` -- for the recorder itself. A Teams/Zoom/Meet job, a note-taker
    webhook, or a nightly sync posts the transcript onto the Interview and, if it asks for
    it, the agent runs on the spot. This is the endpoint to point a new provider at; the
    provider does not need to know the agent exists.

`get_transcript_state` is what the form calls to decide what the button should say.

Everything about where a transcript comes from lives in Interview Feedback Agent Settings,
not here -- see `flow.flow_hr.tools.interview_feedback` for the resolver.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from flow.flow_hr.tools.interview_feedback import (
    SETTINGS_DOCTYPE,
    interview_feedback_context,
    interview_transcript,
)

# Roles allowed to run the agent by hand. Recruiters and HR own hiring here; an Interviewer
# is included because the draft lands in their own feedback record.
FEEDBACK_ROLES = (
    "S - HR Director (Global Admin)",
    "S - HR L1",
    "S - HR L2",
    "S - HR L3",
    "S - HR L2 Manager",
    "S - HR Supervisor (RM)",
    "Recruiter",
    "Interviewer",
    "System Manager",
)


def _can_run(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return bool(set(frappe.get_roles(user)) & set(FEEDBACK_ROLES))


def _settings_doc():
    return frappe.get_cached_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


@frappe.whitelist()
def get_transcript_state(interview):
    """What the Interview form needs to render the Transcript section: whether the agent is
    switched on, whether a transcript can actually be found, and what it would be written
    into. Read-only -- safe to call on every refresh."""
    frappe.has_permission("Interview", "read", interview, throw=True)

    settings = _settings_doc()
    state = {
        "interview": interview,
        "enabled": bool(cint(settings.enabled)),
        "allowed": _can_run(),
        "write_mode": settings.write_mode,
        "sources_configured": len([r for r in settings.transcript_sources if cint(r.enabled)]),
        "agent": settings.agent,
    }

    if not state["enabled"] or not state["sources_configured"]:
        state["transcript"] = None
        return state

    found = interview_transcript(interview=interview, limit=1000)
    transcript = found.get("transcript") or {}
    state["transcript"] = (
        {
            "source": transcript.get("source"),
            "format": transcript.get("format"),
            "chars": transcript.get("total_chars"),
            "turns": transcript.get("turns"),
            "usable": transcript.get("usable"),
            "speakers": transcript.get("speakers"),
            "warnings": transcript.get("warnings"),
        }
        if found.get("found")
        else None
    )
    state["attempts"] = found.get("attempts")

    context = interview_feedback_context(interview=interview)
    state["rubric_skills"] = [s["skill"] for s in context["rubric"]["skills"]]
    state["feedback_records"] = context["feedback_records"]
    state["guards"] = context["guards"]
    return state


@frappe.whitelist()
def generate_feedback_from_transcript(interview, source=None, interviewer=None, enqueue=1):
    """Run the agent against one Interview's transcript.

    Refuses before the run rather than letting the agent discover it: a disabled setting, no
    configured source, a cancelled Interview, or a transcript that cannot be found are all
    answered here, where the person who clicked is still looking at the screen. A Flow Run
    that ends in "there was no transcript" reads like a failure and costs a model call.
    """
    if not _can_run():
        frappe.throw(_("You are not allowed to run the Interview Feedback Agent."), frappe.PermissionError)
    frappe.has_permission("Interview", "read", interview, throw=True)

    settings = _settings_doc()
    if not cint(settings.enabled):
        frappe.throw(_("The Interview Feedback Agent is disabled in {0}.").format(SETTINGS_DOCTYPE))
    if not settings.trigger:
        frappe.throw(_("No Flow Trigger is set in {0}.").format(SETTINGS_DOCTYPE))

    doc = frappe.get_doc("Interview", interview)
    if doc.docstatus == 2:
        frappe.throw(_("Interview {0} is cancelled.").format(doc.name))

    found = interview_transcript(interview=interview, source=source, limit=1000)
    if not found.get("found"):
        frappe.throw(
            _("No transcript could be found for {0}. Sources tried: {1}").format(
                doc.name, _describe_attempts(found.get("attempts"))
            )
        )
    transcript = found["transcript"]
    if not transcript.get("usable"):
        frappe.throw(
            _("The transcript for {0} is only {1} characters -- too short to assess anyone on.").format(
                doc.name, transcript.get("total_chars")
            )
        )

    from flow.triggers import fire_manual

    run = fire_manual(
        settings.trigger,
        target_doctype="Interview",
        target_name=doc.name,
        enqueue=cint(enqueue),
    )

    return {
        "interview": doc.name,
        "agent": settings.agent,
        "trigger": settings.trigger,
        "run": run,
        "queued": bool(cint(enqueue)),
        "source": transcript.get("source"),
        "transcript_chars": transcript.get("total_chars"),
        "write_mode": settings.write_mode,
    }


@frappe.whitelist()
def ingest_transcript(
    interview,
    transcript=None,
    file_url=None,
    file_name=None,
    external_ref=None,
    source_label=None,
    run_agent=0,
):
    """Land a transcript on an Interview from outside, then optionally run the agent.

    This is the seam for a provider integration. Pass `transcript` as text, or `file_url`
    for a file already on this site, or a base64 `transcript` with a `file_name` to have it
    attached. `external_ref` is the provider's own id for the recording -- store it and an
    HTTP Endpoint source can fetch by it later without this call carrying the text at all.

    Kept deliberately dumb: it stamps the Interview and stops. Deciding whether the
    transcript is good enough to assess anyone on belongs to the agent's own tooling, not to
    an ingest endpoint.
    """
    if not _can_run():
        frappe.throw(_("You are not allowed to file interview transcripts."), frappe.PermissionError)
    frappe.has_permission("Interview", "write", interview, throw=True)

    doc = frappe.get_doc("Interview", interview)
    if doc.docstatus == 2:
        frappe.throw(_("Interview {0} is cancelled.").format(doc.name))

    stored = {}
    if file_name and transcript:
        attachment = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": file_name,
                "attached_to_doctype": "Interview",
                "attached_to_name": doc.name,
                "content": transcript,
                "decode": True,
                "is_private": 1,
            }
        ).insert(ignore_permissions=True)
        stored = {"file": attachment.name, "file_url": attachment.file_url}
    elif file_url:
        stored = {"file_url": file_url}
    elif transcript:
        doc.db_set("custom_transcript_text", transcript, update_modified=False)
        stored = {"chars": len(transcript)}
    elif not external_ref:
        frappe.throw(_("Pass a transcript, a file_url, or an external_ref."))

    if file_url:
        doc.db_set("custom_transcript_file", file_url, update_modified=False)
    if external_ref:
        doc.db_set("custom_transcript_external_ref", external_ref, update_modified=False)

    doc.db_set("custom_transcript_source", source_label or frappe.session.user, update_modified=False)
    doc.db_set("custom_transcript_received_on", now_datetime(), update_modified=False)

    result = {"interview": doc.name, "stored": stored, "external_ref": external_ref}

    if cint(run_agent):
        # ? fire_manual enqueues after commit, so the stamps above are in place before the
        # ? worker picks the run up.
        result["run"] = generate_feedback_from_transcript(interview=doc.name)

    return result


def _describe_attempts(attempts):
    if not attempts:
        return _("none -- no transcript sources are configured")
    return "; ".join(
        f"{a.get('source')} ({a.get('status')}: {a.get('detail')})" for a in attempts
    )
