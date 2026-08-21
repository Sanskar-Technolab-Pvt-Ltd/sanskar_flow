# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class InterviewFeedbackAgentSettings(Document):
    def validate(self):
        self.validate_rating_scale()
        self.validate_windows()

    def validate_rating_scale(self):
        """`prompt_hr.py.interview_feedback.on_update` computes the obtained score as
        sum(rating_given) / sum(rating_scale) * 10. A scale of 0 on every row makes that a
        division by zero on save, so this default is the last line of defence for an
        Interview Type whose Expected Skill Set left the scale blank."""
        if cint(self.default_rating_scale) < 1:
            frappe.throw(_("Default Rating Scale must be at least 1."))

    def validate_windows(self):
        if cint(self.max_transcript_chars) < 1000:
            frappe.throw(_("Maximum Characters Per Read must be at least 1000."))
        if cint(self.min_transcript_chars) >= cint(self.max_transcript_chars):
            frappe.throw(
                _("Minimum Transcript Characters must be smaller than Maximum Characters Per Read.")
            )
