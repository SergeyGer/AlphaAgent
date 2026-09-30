# Security Policy

AlphaAgent executes financial transactions autonomously. Security issues are
taken seriously and will be acknowledged quickly.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Report privately through GitHub's
[Security Advisories](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
tab (*Security → Report a vulnerability*).

Please include:

- a description of the issue and its impact,
- reproduction steps or a proof of concept,
- affected version / commit,
- any suggested remediation.

You can expect an initial response within **72 hours**. Please allow time for a
fix and release before public disclosure.

## Threat model

The platform has three unusually sensitive properties. Reports touching them are
prioritised:

1. **Trading guardrails must not be bypassable.** `evaluate_proposal()` in
   `tasks.py` is the only path from an AI proposal to a database mutation. Any
   input that causes a trade to exceed `max_trade_allocation_pct`, exceed the
   available cash balance, sell more than the held position, or execute after
   `daily_loss_limit_usd` has been breached, is critical.
2. **The AI layer must never write to the database.** CrewAI tools are read-only
   by design. A prompt-injection route that turns tool output into a database
   mutation or a trade is critical.
3. **Untrusted input is parsed in the task path.** RSS feeds are attacker
   controllable; they are parsed with `defusedxml` to block entity-expansion and
   external-entity attacks.

## Handling of secrets

**Never commit credentials.** `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` are
mandatory environment variables, and `config/settings.py` deliberately contains
no credential fallbacks — it raises `ImproperlyConfigured` when they are absent.

`.env` is gitignored. `core/tests/test_security.py` scans every publishable file
for credential patterns on each CI run, so a leaked secret fails the build.

If you believe a credential has been exposed:

1. Rotate it at the provider immediately (assume it is compromised).
2. Purge it from git history (`git filter-repo`), not just from the latest commit.
3. Audit `AgentDecisionLog` and `Transaction` for unexpected activity.

## Deployment checklist

- [ ] `DJANGO_DEBUG=false`
- [ ] Strong, unique `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`
- [ ] `DJANGO_ALLOWED_HOSTS` restricted to real hostnames
- [ ] TLS terminated in front of the app, with
      `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SESSION_COOKIE_SECURE`,
      `DJANGO_CSRF_COOKIE_SECURE` and `DJANGO_SECURE_HSTS_SECONDS` enabled
- [ ] PostgreSQL and Redis not exposed to the public internet
- [ ] `AI_ALLOW_HEURISTIC_FALLBACK=false` if trades must be model-driven
- [ ] Log aggregation enabled for `logs/errors.log`
- [ ] Database backups verified
