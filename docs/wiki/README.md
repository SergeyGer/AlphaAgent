# Wiki source

These are the **source of truth** for the GitHub Wiki. They live in the
repository so that a documentation change travels in the same pull request as the
behaviour change it describes, and can be reviewed and link-checked like any other
file.

The GitHub Wiki is a published mirror. Nothing is edited there directly — an edit
made in the web UI is overwritten the next time this directory is pushed.

## Why the source lives here

The wiki used to be authored only in the wiki repository, which had two
consequences worth avoiding:

- **A change could not be reviewed.** The wiki repo is separate from the code, so
  a behaviour change and its documentation update could not appear in one diff.
  A reviewer saw one half.
- **Nothing checked it.** No CI ran against wiki pages, so a link to a screenshot
  that had been renamed, or to a page that had been retitled, rotted silently.

Keeping the text here fixes both: `docs.yml` link-checks every page and verifies
every referenced media file exists, and a pull request that changes behaviour can
update the page in the same commit.

## Publishing

```bash
make wiki-push    # mirrors docs/wiki/ to the GitHub wiki
```

The target clones `AlphaAgent.wiki.git`, replaces its contents with this
directory, and pushes. `_Sidebar.md` and `_Footer.md` are the special filenames
GitHub uses for the navigation sidebar and the page footer; the sidebar is
maintained by hand in `_Sidebar.md` and new pages must be added to it.

## Conventions

- One page per topic, `Title-Case-With-Dashes.md`. The filename becomes the URL.
- The first heading is the page title and must match the filename.
- Link between pages with relative links (`[Architecture](Architecture)`), never
  absolute `https://github.com/.../wiki/...` URLs — absolute links break when the
  repository is forked or renamed, and the link checker has no way to tell a
  stale one from a fresh one.
- If a page embeds an image, the image must exist under `docs/` in this
  repository. `docs.yml` enforces that.
