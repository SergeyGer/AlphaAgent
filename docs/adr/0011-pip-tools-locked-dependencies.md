# 0011. Lock Python dependencies with pip-tools so the full tree is visible

- **Status:** Accepted
- **Date:** 2026-10-06
- **Deciders:** Project maintainer

## Context

Dependencies were a hand-written list of 16 direct pins. That installs, but it
leaves the resolved tree untracked, and GitHub builds its dependency graph from
manifest files — so it could only see the packages named directly. The measured
result: **34 of 201 installed packages were visible and 183 were not**, including
`chromadb`, which carries four advisories. Enabling security alerts therefore
reported almost nothing, not because the tree was clean but because most of it was
invisible.

The frontend already had the equivalent property through
`frontend/package-lock.json`, which made all 219 npm packages visible.

Rejected alternatives, recorded in the wiki:

- *Keep hand-written pins and rely on alerts for direct dependencies only.* This
  is the state that hid 183 packages and four advisories.
- *Pin the whole tree by hand.* Unmaintainable, and gives no hash verification.

The date above is the commit that introduced the lockfiles (`f787374`).

## Decision

`requirements.txt` and `requirements-dev.txt` are **generated lockfiles**, compiled
from `requirements.in` and `requirements-dev.in` by `pip-tools` (`make lock` and
`make lock-dev`), and committed. Every artefact is pinned by `==` and by sha256,
and the image build, CI and `make install-dev` all install with
`--require-hashes`. The dev lockfile is compiled too — not left as a thin
`-r requirements.txt` plus two tool pins — and is generated with `--allow-unsafe`
so `pip` and `setuptools` are pinned as well. Contributors edit the `.in` files
only.

## Consequences

### Positive

- Full hash verification: a tampered or substituted package fails the install
  rather than executing.
- The Python supply chain became visible to Dependabot and GitHub advisories — all
  180 Python packages, exactly as the npm lockfile already exposed 219. The
  previously invisible `chromadb` advisories surfaced immediately.
- Installs are reproducible: the same lockfile resolves to the same artefacts
  everywhere.
- Ignoring or triaging an update is an explicit, recorded act in
  `dependabot.yml` rather than a silent gap.

### Negative / trade-offs

- Lockfile churn is large. A one-line `.in` change rewrites a file of hundreds of
  thousands of lines, so diffs are reviewed by intent, not content.
- The generated files must not be edited. A direct edit to `requirements.txt` is
  silently overwritten by the next `make lock`.
- The dev lockfile duplicates the entire runtime tree, which is inherent to
  pip-tools. A single advisory therefore appears twice and inflates alert counts;
  `chromadb` showed up as "4 advisories × 2 manifests".
- Hash-checking mode is all-or-nothing: as soon as one requirement carries a hash,
  every requirement must. Getting this wrong fails CI with "Hashes are required in
  `--require-hashes` mode".
- `--allow-unsafe` is mandatory, and omitting it fails only in a clean container
  where `setuptools` is absent — it does not reproduce on a developer machine.
- Monthly grouped updates still require triage. The config carries documented
  ignores for bumps that cannot install (`openai >= 3.0.0`, `pydantic >= 2.13.0`,
  `Django >= 6.0.0`, `python >= 3.14`).

### Neutral

- One genuine risk is accepted rather than fixed: `chromadb` has four advisories
  with no patched release, `crewai` pins `chromadb~=1.1.0`, and this project never
  imports it, runs no Chroma server and builds its Crew with `memory=False`. The
  triage is recorded in `dependabot.yml` with a revisit trigger.
