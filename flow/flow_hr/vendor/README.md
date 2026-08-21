# Fixes this pack needs in other apps

Three of the agents call into `prompt_hr`, and two of those call paths are broken on
hrms 16 as shipped. The fixes are `prompt_hr`'s own code, so they cannot live in this app —
but the agents that depend on them do, so the patch travels here.

Base commit: `aa6a1788598e08ff2165634c2bed65ab0b7e68b3` on
`Sanskar-Technolab-Pvt-Ltd/prompt_hr`.

```bash
cd apps/prompt_hr
git apply --check ../flow/flow/flow_hr/vendor/prompt_hr-hrms16-compat.patch  # dry run
git apply         ../flow/flow/flow_hr/vendor/prompt_hr-hrms16-compat.patch
```

Then `bench --site <site> migrate && bench build`.

`flow.flow_hr.vendor.check()` reports whether the site is running patched code:

```bash
bench --site <site> execute flow.flow_hr.vendor.check
```

## What each hunk fixes, and which agent needs it

| File | Fix | Needed by |
|---|---|---|
| `py/job_offer.py` | `SalarySlip.get_component_abbr_map` / `_safe_eval` moved to `hrms.payroll.utils` in v16. The old import path raises `ImportError` at module load, so every Job Offer salary annexure fails. | Onboarding Agent |
| `prompt_hr/doctype/employee_standard_salary/employee_standard_salary.py` | Same import move, same failure, on the Employee Standard Salary form. | Payroll Agent |
| `overrides/salary_slip_override.py` | Restores `_salary_structure_doc` and sanitises the structure's formulas before eval — without it a Salary Slip preview raises before it can return figures. | Payroll Agent (`preview_salary_slip`) |
| `py/full_and_final_statement.py` | A commented-out `def` swallowed the body of the next function, so F&F insert and submit both failed. | Offboarding Agent |
| `py/leave_application.py` | `add_days(today(), …)` returns a string; comparing it to `doc.from_date` (a `date`) raised `TypeError`, so enabling backdated-leave limits killed every Leave Application insert. | Attendance Agent |
| `hooks.py` | Unhooks `Full and Final Statement / before_insert`, whose handler is commented out in the module. | Offboarding Agent |

## Nothing else needs patching

`hrms`, `erpnext` and `frappe` are stock. Their only working-tree changes on the bench this
was captured from were `yarn.lock` churn.
