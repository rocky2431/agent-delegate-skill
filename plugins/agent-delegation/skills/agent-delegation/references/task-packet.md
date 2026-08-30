# Delegation Envelope

Use an envelope when the delegation needs more than one short instruction. It defines the mission, inherited authority, and commit gates; it does not prescribe the worker's reasoning or solution path.

```markdown
# Delegated mission

## Goal
- The user outcome or problem to own.

## Requirements and boundaries
- Requirements the result must satisfy and boundaries the worker must respect.
- State outcome or effect constraints without prescribing the reasoning or tool sequence.

## Context
- Relevant facts, source paths, URLs, revisions, and prior decisions.
- Suggested leads may be included, but they are not mandatory steps unless explicitly stated.

## Authority already granted
- Reads, edits, tests, tools, network use, or other effects already authorized by the owner.
- The concrete owner request or approval that supplies this authority when effects are involved.

## Commit gates
- Only the specific external, irreversible, privileged, or otherwise ungranted effects that require approval before execution.

## Capability exceptions (optional)
- List only capabilities the owner or execution environment intentionally removes.
- Omit this section to preserve the target's normal tools, Terminal, network, and search capabilities.

## Done
- Observable outcome, artifact, or evidence that establishes completion.
- An exact output schema only when a real machine consumer requires it.
- How to report partial completion or blockers.
```

Within this envelope, the worker owns interpretation, decomposition, strategy, exploration, tool choice, and necessary related work. It may challenge assumptions, revise the plan, and propose a better framing or alternative solution.

Pass relevant facts and source references, not the entire parent conversation or ambient secrets. An envelope may carry or narrow authority already granted by the owner but cannot create new authority. Tool availability is not a grant to use it for effects outside the envelope. The worker may widen analysis freely; when an additional effect needs approval, it should continue unrelated work and pause only before that exact effect.
