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
- [ ] No credentials, keys or real passwords are included — in code, config **or** test fixtures
- [ ] Any new environment variable is documented in `.env.example` and the README
- [ ] Model changes include a migration (`makemigrations`)

### If this touches trading behaviour

- [ ] I added or updated cases in `core/tests/test_guardrails.py`
- [ ] Allocation, stop-loss and position limits are still enforced
- [ ] The AI layer still cannot write to the database (tools remain read-only)
- [ ] Every outcome — executed, HOLD, blocked, crashed — still writes an `AgentDecisionLog` row

## Screenshots / example output

<!-- Optional, but helpful for API or CLI changes. Redact secrets. -->
