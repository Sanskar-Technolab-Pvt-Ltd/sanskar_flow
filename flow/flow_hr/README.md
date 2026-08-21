# Flow HR — the HR agent pack

Nine agents that run HR processes on this bench, the tools they call, the triggers that start
them, and an installer that puts all of it on a site.

Everything in here is either **code** (the tools, the two config doctypes, the desk buttons)
or **data** (`fixtures/`: the agents, their instructions, their triggers, their knowledge).
Code arrives with `bench migrate` like any app. Data cannot — so it is installed by
`install.py`, from two entry points:

| When | What runs |
|---|---|
| `bench install-app flow` | the `after_install` hook → `flow.flow_hr.install.after_install` |
| `bench migrate` on a site that already has flow | `flow.patches.v1_0.install_hr_agent_pack` |

Both call the same idempotent `install()`. A fresh install never reaches the patch —
`frappe.installer.install_app` marks an app's own patches as completed *before* it calls
`after_install` — which is exactly why both exist.

```bash
bench --site <site> execute flow.flow_hr.install.install     # create or refresh the pack
bench --site <site> execute flow.flow_hr.install.status      # what is installed vs. the fixtures
bench --site <site> execute flow.flow_hr.install.uninstall   # remove agents, triggers, tools
bench --site <site> execute flow.flow_hr.vendor.check        # are the prompt_hr fixes applied?
```

## What install() rewrites, and what it writes once

| Rewritten every run | Written once, then left alone |
|---|---|
| agent instructions | whether an agent or trigger is `enabled` |
| tool descriptions and `import_path` | the transcript source list |
| trigger prompt templates and conditions | knowledge source content |
| | API keys — never written at all |

The left column is what gets tuned in this repo and should travel. The right column is what
the site's HR team owns; clobbering it on every migrate would undo their work — including the
deliberate decision to leave a trigger switched off.

## The agents

| Agent | Starts on | Model |
|---|---|---|
| Onboarding Agent | Job Offer `on_update` | GPT-4.1 |
| Employee Activation Agent | Candidate Portal `on_update` (documents collected) | GPT-4.1 |
| Resume Shortlisting Agent | Job Applicant `after_insert` | Mistral |
| Interview Scheduling Agent | Job Applicant `on_update` (shortlisted) | Mistral |
| Interview Feedback Agent | **button** on Interview → Manual trigger | GPT-4.1 |
| Attendance Agent | Leave Application `after_insert` | GPT-4.1 |
| Payroll Agent | no trigger — read-only, run from the panel | GPT-4.1 |
| Offboarding Agent | **button** on the Employee Exit tab → Manual trigger | Mistral |
| HR Helpdesk Agent | no trigger — answers from the HR Policies knowledge base | Mistral |

Two of them hang off a button rather than a doc event, because their input does not exist when
the document is saved: a transcript arrives from the recorder minutes or hours later, and
offboarding needs a relieving date that HR has to type. Both go through
`flow.triggers.fire_manual()` and the `Manual` Flow Trigger event type.

`Offboarding Agent - Employee Separation Created` ships **disabled** — it is the old
doc-event route, kept as a record of why the button replaced it. The agent found
`relieving_date` still blank and so always skipped the Exit Interview and the F&F Statement.

## Layout

```
flow_hr/
  install.py          the installer both entry points call
  fixtures/           agents, tools, triggers, knowledge, models — the data
  tools/              the HR tool implementations the Flow Tool rows import
  doctype/            Interview Feedback Agent Settings, Interview Transcript Source
  offboarding.py      whitelisted methods behind the Employee Exit-tab button
  interview_feedback_agent.py   whitelisted methods behind the Interview button
  demo/               seeders for the data a fresh bench does not have
  vendor/             fixes this pack needs in prompt_hr, as an applyable patch
```

The desk-side halves of the two buttons are `flow/public/js/employee_offboarding.js` and
`flow/public/js/interview_feedback_agent.js`, wired through `doctype_js` in `hooks.py`.

## What the pack depends on

- **hrms** — required. Without `Employee`, `Job Applicant`, `Job Offer`, `Interview` and
  `Leave Application`, `install()` skips the pack entirely and says so rather than failing.
- **prompt_hr** — required by three agents. The Employee Activation Agent's trigger watches
  `Candidate Portal`; the Onboarding and Interview Scheduling agents call whitelisted
  prompt_hr methods. Its own fixes are in `vendor/`.
- **API keys** — not in this repo. A Flow Model created by `install()` is left **disabled**
  until somebody sets a key on it or on its Flow Provider; `status()` names the ones waiting.
- **The HR policy documents** — only the FAQ text sources travel here. The PDF behind
  `Document : HR Policies` has to be attached to the knowledge base on the site.
