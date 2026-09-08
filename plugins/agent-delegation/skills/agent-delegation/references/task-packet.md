# Mission context

A handoff can be ordinary prose. Include what the worker needs to own the outcome:

- The goal and observable completion evidence.
- Relevant facts, files, revisions, and decisions.
- Requirements and authority the owner already granted.
- Any specific effect or capability exception that matters to this task.
- Existing acceptance IDs and their full meanings, exclusions, corrections and
  failed attempts; source paths and the exact receiving workspace, including
  uncommitted artifacts that a new worktree would not contain.
- The selected working record and its writer, if one exists. Workers return evidence
  for that writer instead of maintaining a competing next-action list.

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

Ask the receiver to read the supplied result and state before acting. Preserve settled
terms and existing authority; ask only about a material unresolved decision. Return
actual artifact paths, check commands/results, unfinished requirements and any unknown
effects. Keep the wrapper's delegation ID and terminal receipt with those references.
A normal model-turn stop is not the consumer's business verdict. No exact schema,
separate handoff file or installed companion Skill is required for a prose consumer.
