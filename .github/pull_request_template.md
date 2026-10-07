# Pull Request

## Summary

<!-- What does this change, and why? Link the issue it closes. -->

Closes #

## Type of change

- [ ] Bug fix (non-breaking change that resolves an issue)
- [ ] New feature (non-breaking change that adds capability)
- [ ] Breaking change (existing behaviour changes)
- [ ] Documentation only
- [ ] Refactor / internal cleanup
- [ ] CI, tooling or dependency update

## Affected area

- [ ] REST API
- [ ] AI layer (CrewAI, `ai_agent.py`)
- [ ] Celery tasks / scheduling (`tasks.py`)
- [ ] Execution guard or risk limits
- [ ] Models / migrations
- [ ] Services (market data, news, ledger)
- [ ] Docker / deployment
- [ ] Documentation

## How was this tested?

<!-- Commands you ran, plus any manual verification. -->

```
make check
```

## Checklist

- [ ] `make check` passes locally (lint, format, Django checks, migration drift, tests)
- [ ] Tests were added or updated for the change
- [ ] Coverage did not fall below the CI floor, and was raised if the change added tests
- [ ] No credentials, keys or real passwords are included — in code, config **or** test fixtures
- [ ] Any new environment variable is documented in `.env.example` and the README
- [ ] Model changes include a migration (`makemigrations`)

### Documentation follows the behaviour

The rule in [CONTRIBUTING.md](../CONTRIBUTING.md#documentation-is-part-of-the-change)
is that a behaviour change updates its page **in this pull request**. The `Docs`
job link-checks every Markdown file and verifies referenced images exist, but it
cannot tell whether the prose is still *true* — only a human can.

- [ ] Any behaviour change here is reflected in the matching `docs/wiki/` page
      (Execution-Guard, Configuration, API-Reference, Data-Model, Agent-Debate, Operations …)
- [ ] This is **not** a behaviour change, so no wiki page needed updating
- [ ] If this makes or reverses an architectural decision: a **new** file in
      `docs/adr/` (next number — accepted ADRs are superseded, never edited)
- [ ] If `docs/wiki/` changed: `make wiki-push` has been run, or will be after merge

### If this touches trading behaviour

- [ ] I added or updated cases in `core/tests/test_guardrails.py`
- [ ] Allocation, stop-loss and position limits are still enforced
- [ ] The AI layer still cannot write to the database (tools remain read-only)
- [ ] Every outcome — executed, HOLD, blocked, crashed — still writes an `AgentDecisionLog` row

## Screenshots / example output

<!-- Optional, but helpful for API or CLI changes. Redact secrets. -->
