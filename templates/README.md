# Decision templates

A template describes one kind of decision in the Case / Evidence / Decision format:
- an example case: one text;
- suggested tags for the case;
- the evidence fields: typed facts;
- the decisions: one question each, Yes / no, Choice or Score;
- optionally, fictional example values.

The New decision page and `TypeLogic.bat cli templates` read them from three places. A later location replaces an earlier template with the same id:

| Folder | Contents |
|---|---|
| `templates/builtin/` | Shipped with TypeLogic. The app never changes them. |
| `templates/custom/` | Templates saved from the app into this project folder, so they can be shared (for example with git). |
| `~/.typelogic-ai/templates/` | The same saved templates in your data folder. |

"Save as template" in the app writes both the project copy and the data-folder copy.

## Format (`typelogic-template-v1`)

```json
{
  "format": "typelogic-template-v1",
  "id": "execution-safety",
  "title": "Agents · tool call safety check",
  "description": "Decide whether a proposed tool call may run without human approval.",
  "example": true,
  "case": "Task: clean up inactive accounts ... Context: ... no backup was taken today.",
  "tags": ["security", "it-support"],
  "evidence_schema": [
    {"name": "row_count", "type": "integer", "description": "Rows in the target table"},
    {"name": "backup_taken_today", "type": "boolean", "description": "A backup was taken today"},
    {"name": "urgency", "type": "enum", "options": ["low", "medium", "high"]}
  ],
  "evidence": {"row_count": 48210, "backup_taken_today": false},
  "decisions": [
    {"output": "boolean",
     "instructions": "Is this tool call safe to run without human approval?"}
  ],
  "threshold": 0.85,
  "permutation_check": true
}
```

**Evidence types:** `boolean`, `number`, `integer`, `date` (YYYY-MM-DD), `text`, `enum` (needs `options`). A field can also have `unit` and `required`.

**Decisions** need no name; they are answered as `decision_1`, `decision_2`, ... in order. The outputs follow Jev:
- `boolean` (Yes / no): the question only. (`criteria` with `true` / `false` descriptions is still accepted from your own software; the app does not use it.)
- `choice`: `criteria` maps each option to when to choose it. The app and the built-in templates number the options `"0"`, `"1"`, `"2"`, ...; other names are accepted. `abstain_option` names the option that means "not enough information".
- `score`: `criteria` lists the levels, from lowest to highest. The score is the expected level, starting at 0.

**Tags** (optional): up to 8, filled into the tag field when the template is selected. They are stored in lowercase; the suggested tags are `billing`, `refund`, `fraud`, `security`, `compliance`, `legal`, `support`, `technical`, `sales`, `purchasing`, `logistics`, `inventory`, `hr`, `finance`, `it-support`, `urgent`, `low-priority`, `escalation`, `customer` and `internal`, and any other tag of up to 32 characters is accepted.

The case title is not part of a template: it is typed for each case, or generated after the evaluation.

Templates saved by earlier versions still load: a `case` with `summary` and `details` becomes one text, and `rules`, `exact_checks` and the automatic review keys of decisions (`auto_review`, `verdict`) are ignored.

The examples in `builtin/` are fictional. Validate a template on labeled cases from your own domain before you rely on its answers.
