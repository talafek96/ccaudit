# Releasing

Publishing a version of ccost to PyPI. Two one-time setup steps, then one command per
release.

> There was a `rel/stable` branch here until 0.1.0. It existed from when pushing it was what
> triggered the publish; once a published GitHub Release became the trigger, it only fed a
> containment check that `main` answers just as well — the workflow re-runs the whole gate on
> the tagged commit regardless. It was ceremony describing itself. A maintenance line (shipping
> 0.1.x while `main` moves to 0.2) is the one thing that would bring it back.

## The three names

| Name | Is | Where it appears |
|---|---|---|
| `claude-cost-tracker` | the distribution | PyPI, the repo, `pip install`, `uvx` |
| `ccost` | the command | what you type; `[project.scripts]` |
| `claude_cost_tracker` | the import package | `src/`, every import, `python -m` |

They are deliberately different, and the release path touches all three: the trusted publisher
is registered against the *distribution*, the workflow asserts artifact filenames built from the
*import package* (PEP 625 renders `-` as `_`), and `--version` prints the *command*.

## How it works, in one paragraph

**The git tag is the version.** `hatch-vcs` reads it at build time, so there is no number in
a file that can disagree with the tag — "bumped and tagged" is one fact, not two that drift.
`scripts/release.py` tags a commit on `main`, pushes the tag, and leaves a **draft GitHub
Release**.

**Nothing ships until you publish that Release.** Publishing it is what starts
`.github/workflows/release.yml`, which re-runs the full gate on that exact commit, refuses
anything not contained in `main`, waits for your approval, and uploads over OIDC —
**no API token exists anywhere**. A tag on its own publishes nothing, so tags stay cheap.

---

## One-time setup

### Step 1 — create the `pypi` environment on GitHub

This is what makes the publish wait for you, and it is half of what PyPI will trust.

1. Go to **<https://github.com/talafek96/claude-cost-tracker/settings/environments>**
2. **New environment** → name it exactly `pypi` → **Configure environment**
3. Tick **Required reviewers**, add yourself, **Save protection rules**

The name must be exactly `pypi`; the workflow and PyPI both refer to it by that string.

### Step 2 — register the pending publisher on PyPI

`ccost` does not exist on PyPI yet, so this is a *pending* publisher: it reserves the name
and is converted into a real one by the first successful upload.

1. Sign in at **<https://pypi.org>** (enable 2FA if you have not — PyPI requires it to publish)
2. Go to **<https://pypi.org/manage/account/publishing/>**
3. Under **Add a new pending publisher**, choose **GitHub** and fill in *exactly*:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `claude-cost-tracker` |
   | Owner | `talafek96` |
   | Repository name | `claude-cost-tracker` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

4. **Add**

`Workflow name` is the *filename*, not the `name:` inside the file. Getting this wrong is the
most common cause of a publish failing with "not a trusted publisher" — the two are easy to
confuse, because the workflow's `name:` is `release`.

---

## Cutting a release

From a clean checkout of `main`:

```sh
uv run scripts/release.py minor --dry-run   # see exactly what would happen
uv run scripts/release.py minor             # do it
```

`major` / `minor` / `patch`, or an exact version (`uv run scripts/release.py 1.4.2`).

The script refuses, before changing anything, if:

- the working tree is dirty — a release is built from a commit, not from your disk;
- `main` and `origin/main` disagree — you would be releasing something unpushed;
- the tag already exists — a published version is never re-cut.

The script prints a link to the draft Release it created. Then:

1. **Open the draft**, read the generated notes, edit them, and press **Publish release**.
   Until you do, nothing has been built and nothing can reach PyPI.
2. **`verify`** starts automatically: tag shape, containment in `main`, `ruff format
   --check`, `ruff check`, `mypy`, `pytest`, then builds and asserts the artifact's version
   equals the tag.
3. **`publish`** waits for you a second time. Open the run → **Review deployments** → tick
   `pypi` → **Approve and deploy**.
4. It uploads over OIDC and the version appears at <https://pypi.org/p/claude-cost-tracker>.

Two gates, deliberately, and they catch different things: publishing the Release is where you
decide *this is a release*, and approving the deployment is where you decide *after seeing the
gate pass*. If the gate fails, you never get asked the second question.

## After the first release

The README already points at PyPI. Check the one-liner actually resolves:

```sh
uvx claude-cost-tracker    # the zero-install one-liner
uv tool install claude-cost-tracker   # then just `ccost`
```

## What a version means to someone running it

| How they got it | `ccost --version` says |
|---|---|
| `uvx ccost`, `pip install claude-cost-tracker` | `0.1.0` |
| `uvx --from git+…@v0.1.0` | `0.1.0` — identical |
| `uvx --from git+…` (tip of `main`) | `0.1.1.dev7+g1a2b3c4` |

The third row is a feature, not an inconsistency: it names how far past the release that
build is and which commit it came from. A frozen version could not, which is how a stale
cache once hid behind `0.0.0`.

One caveat, recorded because it fails loudly and confusingly: building from a **GitHub zip or
tarball download** has neither `.git` nor `PKG-INFO`, so there is nothing to derive a version
from. `raw-options = { fallback_version = "0.0.0" }` in `pyproject.toml` makes that build
succeed and label itself `0.0.0` rather than erroring. Installing from PyPI, from an sdist, or
from a git URL is unaffected.

## If a publish fails

- **"not a trusted publisher"** — the five fields in Step 2 must match exactly, especially
  `Workflow name` = `release.yml` (the filename) and `Environment name` = `pypi`.
- **"File already exists"** — that version is on PyPI and cannot be replaced. Cut the next
  patch version; PyPI does not allow re-uploading, by design.
- **"is not contained in main"** — the Release was published for a commit that is not on the
  trunk. Release from `main`, using `scripts/release.py`.
- **The `verify` job failed** — nothing was published. Fix it on `main`, then release again;
  the tag from the failed attempt still exists, so use the next version.
- **Nothing happened when I pushed a tag** — that is the design. A tag publishes nothing on
  its own; publish the GitHub Release for it.
- **The release is a pre-release / the tag has a suffix** — only `vMAJOR.MINOR.PATCH` is
  publishable. `v1.2.3-rc1` is refused by the first check, deliberately.
