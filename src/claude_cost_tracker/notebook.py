"""The optional exploration surface: a marimo notebook written out over the payload.

**Why this is a fourth surface and not a replacement.** The shareable report has to be one
self-contained file that opens offline with no tooling (FR-032) — that is the artifact that
goes to someone who is disputing a number. A notebook is not that. So this adds a place to
poke at the data interactively; it does not take anything away.

**Why it costs no dependency.** A marimo notebook *is* a Python file. ccost writes one and
never imports marimo, so the runtime dependency set stays `rich` and nothing else (Principle
II). The notebook declares what it needs in PEP 723 inline metadata, which means

    uvx marimo edit --sandbox ccost-notebook.py

installs marimo and altair into a throwaway environment for that notebook alone. Nothing is
added to the machine and nothing is added to this project.

**Why the notebook shells out to the CLI instead of computing.** The one rule every surface
here obeys is that figures come from one implementation (FR-074). A notebook that added up
per-session numbers in pandas would be a second one, free to disagree. So the reactive cells
run ``ccost --json`` for the current selection and render what comes back: marimo supplies
the interactivity, ccost supplies every number. That is also what makes the session picker
correct rather than approximately correct — reselecting re-runs the real analysis.
"""

import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

DEFAULT_NOTEBOOK = Path("ccost-notebook.py")

# Where the generated notebook learns how to call us back. Substituted at write time, because
# the notebook cannot work it out for itself: it runs under marimo's sandbox interpreter, which
# has marimo and altair in it and no ccost at all.
COMMAND_PLACEHOLDER = "__CCOST_COMMAND__"

# And which corpus it is exploring. Also substituted at write time, and for the same reason the
# command is: the notebook is a separate process that shells back in, so the scope has to travel
# as arguments. It used to be the literal `--all`, which meant `ccost --project X notebook`
# opened a picker over every session on the machine — a different question than the one the
# command asked, from the surface whose whole claim is that it cannot disagree with the terminal.
SCOPE_PLACEHOLDER = "__CCOST_SCOPE__"


def ccost_command() -> list[str]:
    """The argv prefix that reaches *this* ccost from an unrelated process.

    Assuming ``ccost`` is on PATH is wrong for the way most people run this. `uvx --from
    git+... claude-cost-tracker` puts the executable in a uv cache directory and never on PATH, so the
    notebook's first cell died with a bare `FileNotFoundError` on Windows.

    Resolution is most-specific first: the executable that is running now, then one on PATH,
    then this interpreter with ``-m``. Every form is absolute, because the notebook runs with a
    different interpreter, a different environment, and possibly a different working directory.
    """
    launcher = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if launcher is not None and launcher.name.startswith("ccost") and launcher.is_file():
        return [str(launcher.resolve())]
    on_path = shutil.which("ccost")
    if on_path:
        return [str(Path(on_path).resolve())]
    # `python -m claude_cost_tracker` works wherever the package is importable, which is the case whenever
    # this code is the thing running.
    return [str(Path(sys.executable).resolve()), "-m", "claude_cost_tracker"]


# PEP 723 metadata, so the notebook carries its own environment. `marimo edit --sandbox` reads
# it and builds a throwaway venv; the user installs nothing permanently and ccost depends on
# neither package.
NOTEBOOK_SOURCE = '''# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo", "altair", "pandas", "pyarrow"]
# ///
"""claude-cost-tracker — interactive exploration.

Written by `ccost notebook`. Run it with:

    uvx marimo edit --sandbox ccost-notebook.py

Every figure in here is produced by ccost itself: the cells below shell out to
`ccost --json` and render the result. Nothing recomputes a cost, which is why the numbers
here and the numbers in the terminal cannot disagree.

Figures are API-equivalent cost estimates imputed from published list rates. They are not
billed amounts.
"""

import marimo

app = marimo.App(width="medium")


@app.cell
def _():
    import json
    import subprocess

    import altair as alt
    import marimo as mo
    import pandas as pd

    # pandas because altair consumes a pandas frame directly. pyarrow is not imported here but
    # is declared above: marimo hands chart data to the browser as Arrow and, without it, logs
    # a fallback warning on every chart. Harmless on macOS — on Windows, writing that warning
    # from marimo's spawned worker fails with `OSError: [WinError 1] Incorrect function` and
    # prints a full traceback under a chart that rendered perfectly well.
    return alt, json, mo, pd, subprocess


@app.cell
def _(json, subprocess):
    # Written in by `ccost notebook`, absolute. Not the bare name "claude-cost-tracker": this notebook
    # runs under marimo's sandbox interpreter, and a ccost installed by `uvx` is not on PATH
    # at all — which failed here with an unexplained FileNotFoundError.
    CCOST = __CCOST_COMMAND__
    # The corpus this notebook was opened over — `["--all"]`, or the project that was named.
    # Written in by `ccost notebook` so the picker below asks about the same sessions the
    # command did.
    SCOPE = __CCOST_SCOPE__

    def _run(args):
        try:
            return subprocess.run(
                [*CCOST, *args], capture_output=True, text=True, check=False
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"cannot run ccost at {CCOST[0]!r}. It was there when this notebook was "
                f"written; if ccost has since moved, regenerate the notebook with "
                f"`ccost notebook`, or install it permanently with "
                f"`uv tool install --from git+https://github.com/talafek96/ccost`."
            ) from error

    def run_ccost(*args):
        """Ask ccost for a payload. The only source of a figure in this notebook."""
        result = _run([*args, "--json"])
        if result.returncode != 0:
            raise RuntimeError(
                f"ccost exited {result.returncode}: {result.stderr.strip() or 'no detail'}"
            )
        return json.loads(result.stdout)

    def list_sessions(facts=False):
        """Session ids paired with their names, newest first.

        With ``facts`` the listing also carries what each session can be ranked by — cost,
        rounds, reads, .md files, skills. That analyses every session, so it is opt-in.
        """
        result = _run(["sessions", *SCOPE, "--json"] + (["--facts"] if facts else []))
        if result.returncode != 0:
            raise RuntimeError(
                f"ccost exited {result.returncode}: {result.stderr.strip() or 'no detail'}"
            )
        return json.loads(result.stdout)

    return CCOST, SCOPE, list_sessions, run_ccost


@app.cell
def _(list_sessions, mo, pd):
    # Measured up front here, unlike the browser view: a notebook cell is expected to take a
    # moment, and having the columns present is what lets the table be sorted by them at all.
    sessions = list_sessions(facts=True)
    session_frame = pd.DataFrame(
        [
            {
                "session": row["display_name"],
                # `None`, not 0, when the session carries no measured facts — a session this
                # rate table cannot price has an *unknown* cost, and a $0.00 in this column
                # would read as a free session and sort like one (Principle X).
                "cost ($)": (
                    None
                    if row.get("cost_micros") is None
                    else round(row["cost_micros"] / 1_000_000, 2)
                ),
                "rounds": row.get("turns"),
                "reads": row.get("reads"),
                ".md reads": row.get("md_reads"),
                ".md files": row.get("md_files"),
                "skills": row.get("skills"),
                "items": row.get("items"),
                "records": row["record_count"],
                "project": row["project"],
                "session_id": row["session_id"],
            }
            for row in sessions
        ]
    )
    # Sort any column by clicking it; tick the rows to analyse. Selection *is* the picker —
    # one control, so what is ranked and what is analysed cannot drift apart.
    ranked = session_frame.sort_values("cost ($)", ascending=False).reset_index(drop=True)
    picker = mo.ui.table(
        ranked,
        # Opens on the costliest few rather than on nothing: an empty table would make the
        # notebook's first view an instruction instead of an answer.
        initial_selection=list(range(min(len(ranked), 8))),
        selection="multi",
        label="Sessions in the analysis — click a column to rank by it, tick rows to include",
    )
    picker
    return picker, session_frame, sessions


@app.cell
def _(mo):
    # Grouping is a real feature of the tool that was reachable only through `--help`, so the
    # question "how do I see this by folder?" had no answer on the surface that raises it.
    # A grouping only ever *merges* rows, so every choice sums to the same total.
    grouping = mo.ui.dropdown(
        options=["item", "file", "folder", "ext", "category"],
        value="item",
        label="Group rows by",
    )
    sorting = mo.ui.dropdown(
        options=["cost", "carry", "direct", "reads", "share"],
        value="cost",
        label="Rank rows by",
    )
    mo.hstack([grouping, sorting], justify="start", gap=2)
    return grouping, sorting


@app.cell
def _(grouping, mo, picker, run_ccost, sorting):
    # `mo.ui.table` hands back the selected *rows*, so the ids come out of the frame. Ticking
    # nothing is a state to explain, not an empty analysis to run.
    chosen = list(picker.value["session_id"]) if len(picker.value) else []
    if not chosen:
        mo.stop(True, mo.md("**Select at least one session.** Nothing to analyse yet."))
    payload = run_ccost(
        "--session", *chosen, "--by", grouping.value, "--sort", sorting.value
    )
    totals = payload["totals"]
    mo.md(
        f"""
        ## {payload["scope"]["covered_through_turn"]:,} turns across
        {len(payload["scope"]["sessions_included"])} session(s)

        Rows grouped by **{payload["group_by"]}**, ranked by **{payload["sort_by"]}**.
        {"A folder row is the files sitting *directly* in it, not everything beneath it — "
         "otherwise a file would be counted once for every folder above it."
         if payload["group_by"] == "folder" else ""}

        **Total (API-equivalent estimate): ${totals["cost_micros"] / 1e6:,.2f}** —
        accounted for ${totals["attributed_micros"] / 1e6:,.2f}
        ({totals["attributed_share"]:.1%}), couldn't attribute
        ${totals["unattributed_micros"] / 1e6:,.2f} ({totals["unattributed_share"]:.1%}).

        These are estimates imputed from published list rates, not billed amounts.
        """
    )
    return payload, totals


@app.cell
def _(payload, pd):
    items = pd.DataFrame(
        [
            {
                "item": row["display"],
                "category": row["category"],
                # The same tags the HTML report puts on a row, so the two surfaces can be
                # filtered by the same words. `uncacheable` is the one that is not a category:
                # the content was too small to cache on some model, and was therefore carried
                # at ten times the usual rate.
                "tags": " ".join(
                    [row["category"]] + (["uncacheable"] if row["never_cacheable_on"] else [])
                ),
                "cost": row["total_micros"] / 1e6,
                "loading": row["direct_micros"] / 1e6,
                "keeping": row["carry_micros"] / 1e6,
                "reads": row["reads"],
                "turns_resident": row["turns_resident"],
                "tokens": row["size_tokens"],
                "share": row["share"],
            }
            for row in payload["items"]
        ]
    )
    return (items,)


@app.cell
def _(items, mo):
    # Filtering by tag, the same words the HTML report puts on each row. Selecting nothing
    # means no filter rather than an empty page — an empty selection reads as "I have not
    # chosen yet", never as "show me nothing".
    tags = sorted({tag for row in items["tags"] for tag in row.split()})
    tag_filter = mo.ui.multiselect(options=tags, value=[], label="Only these tags")
    mo.vstack(
        [
            tag_filter,
            mo.md(
                "_Filtering hides rows, never cost: the total above still covers every item._"
            ),
        ]
    )
    return tag_filter, tags


@app.cell
def _(items, tag_filter):
    wanted = set(tag_filter.value)
    shown = (
        items
        if not wanted
        else items[items["tags"].apply(lambda row: bool(wanted & set(row.split())))]
    )
    return (shown,)


@app.cell
def _(alt, mo, shown):
    # The claim, made clickable: ranking by cost and ranking by read count are different
    # lists. Brush a region to filter the table below it.
    brush = alt.selection_interval()
    plot = (
        alt.Chart(shown)
        .mark_circle()
        .encode(
            x=alt.X("reads:Q", scale=alt.Scale(type="symlog"), title="times read"),
            y=alt.Y("cost:Q", scale=alt.Scale(type="symlog"), title="estimated cost (USD)"),
            size=alt.Size("tokens:Q", title="size (tokens)"),
            color=alt.Color("category:N", title="category"),
            tooltip=[
                "item",
                "tags",
                "cost",
                "loading",
                "keeping",
                "reads",
                "turns_resident",
                "tokens",
            ],
        )
        .add_params(brush)
        .properties(height=380)
    )
    chart = mo.ui.altair_chart(plot)
    mo.vstack(
        [
            mo.md("### Cost against read count\\n\\nDrag to select points; the table follows."),
            chart,
        ]
    )
    return brush, chart, plot


@app.cell
def _(chart, mo, shown):
    selected = chart.value if len(chart.value) else shown
    mo.vstack(
        [
            mo.md(f"### {len(selected)} item(s) selected"),
            mo.ui.table(selected, selection=None),
        ]
    )
    return (selected,)


@app.cell
def _(alt, mo, payload, pd):
    rows = payload.get("sessions", [])
    if not rows:
        mo.stop(True, mo.md("_Per-session comparison needs more than one session._"))
    frame = pd.DataFrame(
        [
            {
                "session": row.get("display_name") or row["session_id"][:8],
                "cause": name,
                "cost": row[field] / 1e6,
            }
            for row in rows
            for name, field in (
                ("loading into context", "direct_micros"),
                ("keeping context loaded", "carry_micros"),
                ("everything else", "other_micros"),
            )
        ]
    )
    mo.vstack(
        [
            mo.md("### What each session cost, and why"),
            mo.ui.altair_chart(
                alt.Chart(frame)
                .mark_bar()
                .encode(
                    x=alt.X("cost:Q", title="estimated cost (USD)", stack="zero"),
                    y=alt.Y("session:N", sort="-x", title=None),
                    color=alt.Color("cause:N", title="cause"),
                    tooltip=["session", "cause", "cost"],
                )
                .properties(height=max(140, 28 * len(rows)))
            ),
        ]
    )
    return frame, rows


@app.cell
def _(mo, payload):
    mo.md(
        "### What these figures do not cover\\n\\n"
        + "\\n".join(f"- {note}" for note in payload["diagnostics"]["limitations"])
    )
    return


if __name__ == "__main__":
    app.run()
'''


def write_notebook(
    path: Path = DEFAULT_NOTEBOOK,
    *,
    command: list[str] | None = None,
    scope: Sequence[str] = ("--all",),
) -> Path:
    """Write the notebook, returning the path written.

    ``scope`` is the argument list the notebook's picker selects its corpus with — ``--all``, or
    the project the reader named. It travels into the file because the notebook is a separate
    process: a notebook opened by ``ccost --project X notebook`` that then listed every
    session on the machine would be answering a question nobody asked.

    The file is written whole, in one call, so an interrupted run leaves no half-written
    notebook that looks complete.
    """
    source = NOTEBOOK_SOURCE.replace(
        COMMAND_PLACEHOLDER, json.dumps(command if command is not None else ccost_command())
    ).replace(SCOPE_PLACEHOLDER, json.dumps(list(scope)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path
