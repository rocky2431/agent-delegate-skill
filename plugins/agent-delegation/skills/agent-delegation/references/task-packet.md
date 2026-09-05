# Mission context

A handoff can be ordinary prose. Include what the worker needs to own the outcome:

- The goal and observable completion evidence.
- Relevant facts, files, revisions, and decisions.
- Requirements and authority the owner already granted.
- Any specific effect or capability exception that matters to this task.

For example:

```text
Investigate why the import loses the final row. Reproduce it and fix the shared
cause in this checkout. Local edits and tests are already authorized. The example
input is in samples/import.csv; inspect other relevant callers as needed. Return
the cause, the change, and validation evidence. Publishing is outside this task.
```

Suggested files or steps are leads unless the owner made them requirements. Use an
exact output schema only for a real machine consumer. A worker may challenge the
framing and choose a better approach within the task's authority. Pass relevant
context instead of copying the entire parent conversation or ambient secrets.
