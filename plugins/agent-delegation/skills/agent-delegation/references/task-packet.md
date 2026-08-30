# Task Packet

Use a packet when the delegation needs more than one short instruction.

```markdown
# Delegated task

## Objective
One independently completable outcome.

## Context and inputs
- Exact source paths, URLs, revisions, or facts the worker may use.

## Included scope
- Work the worker may perform.

## Excluded scope
- Work and side effects the worker must return to the caller.

## Permissions
- Allowed reads, writes, tools, network access, and external effects.
- Owner approval reference when an effect was explicitly authorized.

## Expected result
- Output schema or artifact path.
- Evidence and validation required.
- How to report partial completion or blockers.
```

Pass facts and source references, not the entire parent conversation or ambient secrets. A packet may narrow inherited authority but cannot expand it. The worker must return an explicit incomplete or approval-required result instead of silently widening scope.
