# Copyright (c) 2026, Sanskar Technolab and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.model.document import Document


class InterviewTranscriptSource(Document):
    def validate(self):
        self.validate_required_config()
        self.validate_json_fields()

    def validate_required_config(self):
        """Each source type needs exactly one thing filled in. A row missing it would fail
        silently at resolve time -- the agent would just report 'no transcript found' -- so
        refuse it here, where somebody is looking at the form."""
        required = {
            "Field on Interview": ("fieldname", _("Fieldname")),
            "Python Method": ("method_path", _("Method Path")),
            "HTTP Endpoint": ("endpoint", _("Endpoint")),
        }.get(self.source_type)

        if required and not (self.get(required[0]) or "").strip():
            frappe.throw(
                _("Row {0}: {1} is required for a {2} transcript source.").format(
                    self.idx, required[1], self.source_type
                )
            )

    def validate_json_fields(self):
        for fieldname in ("speaker_map", "headers", "payload"):
            raw = (self.get(fieldname) or "").strip()
            if not raw:
                continue
            try:
                json.loads(raw)
            except ValueError as e:
                frappe.throw(
                    _("Row {0}: {1} is not valid JSON -- {2}").format(
                        self.idx, _(self.meta.get_label(fieldname)), e
                    )
                )
