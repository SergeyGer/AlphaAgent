# CodeQL model pack — sanitiser declarations

This pack tells CodeQL that two functions in this repository **neutralise** their
input, so taint analysis can stop there instead of flowing through them to a
sink.

| Declaration | Function | Kind |
| --- | --- | --- |
| `barrierModel` | `core.log_safety.log_safe` | `log-injection` |
| `barrierModel` | `services.tickers.normalise_ticker` | `log-injection`, `path-injection` |

## Why it is needed

CodeQL reported twelve `py/log-injection` findings on log statements wrapped in
`log_safe()`. The function is a genuine sanitiser — it replaces CR, LF, the C0/C1
control range and the U+2028/U+2029 separators, and bounds the output length —
and `core/tests/test_regressions.py` pins that behaviour. CodeQL had no way to
know: an unmodelled helper is a black box, so taint passes straight through it.

The alternatives were all worse:

- Inlining `.replace("\n", "?")` at twelve call sites duplicates the rule twelve
  times and is *still* unmodelled, so the findings would remain.
- Logging something other than the useful value (a user id instead of a username,
  an exception class instead of its message) discards information an operator
  needs, purely to satisfy a scanner.
- Dismissing the whole rule hides future findings that are genuinely real.

## It is not currently loaded

`codeql-action/init`'s `packs` input accepts only **published** pack references
(`owner/name`). Passing a repository path fails the job with:

```
"./.github/codeql/model-pack" is not a valid pack
```

The pack stays in the tree because it is the correct fix and is one step away.

## Enabling it

Publish the pack (needs `packages: write` on the workflow):

```bash
codeql pack publish ./.github/codeql/model-pack
```

Then reference it from `.github/workflows/codeql.yml`:

```yaml
      - name: Initialize CodeQL
        uses: github/codeql-action/init@v4
        with:
          languages: python
          queries: security-and-quality
          packs: |
            SergeyGer/alphaagent-codeql-models
```

Requires CodeQL 2.25.2 or later for `barrierModel` in data extensions — see the
[GitHub changelog](https://github.blog/changelog/2026-04-21-codeql-now-supports-sanitizers-and-validators-in-models-as-data/)
and the [Python model customisation guide](https://codeql.github.com/docs/codeql-language-guides/customizing-library-models-for-python/).

Until then the affected alerts are dismissed on the Security tab with a comment
naming `log_safe` and its tests, so the reasoning is auditable rather than
implicit.
