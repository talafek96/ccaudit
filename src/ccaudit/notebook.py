"""The optional exploration surface: a marimo notebook written out over the payload.

**Why this is a fourth surface and not a replacement.** The shareable report has to be one
self-contained file that opens offline with no tooling (FR-032) — that is the artifact that
goes to someone who is disputing a number. A notebook is not that. So this adds a place to
poke at the data interactively; it does not take anything away.

**Why it costs no dependency.** A marimo notebook *is* a Python file. ccaudit writes one and
never imports marimo, so the runtime dependency set stays `rich` and nothing else (Principle
II). The notebook declares what it needs in PEP 723 inline metadata, which means

    uvx marimo edit --sandbox ccaudit-notebook.py

installs marimo and altair into a throwaway environment for that notebook alone. Nothing is
added to the machine and nothing is added to this project.

**Why the notebook shells out to the CLI instead of computing.** The one rule every surface
here obeys is that figures come from one implementation (FR-074). A notebook that added up
per-session numbers in pandas would be a second one, free to disagree. So the reactive cells
run ``ccaudit --json`` for the current selection and render what comes back: marimo supplies
the interactivity, ccaudit supplies every number. That is also what makes the session picker
correct rather than approximately correct — reselecting re-runs the real analysis.
"""

from pathlib import Path

DEFAULT_NOTEBOOK = Path("ccaudit-notebook.py")

# PEP 723 metadata, so the notebook carries its own environment. `marimo edit --sandbox` reads
# it and builds a throwaway venv; the user installs nothing permanently and ccaudit depends on
# neither package.
NOTEBOOK_SOURCE = '''# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo", "altair", "pandas"]
# ///
"""ccaudit — interactive exploration.

Written by `ccaudit notebook`. Run it with:

    uvx marimo edit --sandbox ccaudit-notebook.py

Every figure in here is produced by ccaudit itself: the cells below shell out to
`ccaudit --json` and render the result. Nothing recomputes a cost, which is why the numbers
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

    # pandas rather than polars: altair consumes a pandas frame directly, while polars has to
    # cross an Arrow bridge that needs pyarrow — a fourth dependency for no gain at this size.
    return alt, json, mo, pd, subprocess


@app.cell
def _(json, subprocess):
    def run_ccaudit(*args):
        """Ask ccaudit for a payload. The only source of a figure in this notebook."""
        result = subprocess.run(
            ["ccaudit", *args, "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ccaudit exited {result.returncode}: {result.stderr.strip() or 'no detail'}"
            )
        return json.loads(result.stdout)

    def list_sessions():
        lines = subprocess.run(
            ["ccaudit", "sessions", "--all"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
        return [line.split()[0] for line in lines if line[:1].isalnum() and len(line.split()[0]) > 20]

    return list_sessions, run_ccaudit


@app.cell
def _(list_sessions, mo):
    sessions = list_sessions()
    picker = mo.ui.multiselect(
        options=sessions,
        value=sessions[: min(len(sessions), 8)],
        label="Sessions in the analysis",
    )
    picker
    return picker, sessions


@app.cell
def _(mo, picker, run_ccaudit):
    if not picker.value:
        mo.stop(True, mo.md("**Select at least one session.** Nothing to analyse yet."))
    payload = run_ccaudit("--session", *picker.value)
    totals = payload["totals"]
    mo.md(
        f"""
        ## {payload["scope"]["covered_through_turn"]:,} turns across
        {len(payload["scope"]["sessions_included"])} session(s)

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
def _(alt, items, mo):
    # The claim, made clickable: ranking by cost and ranking by read count are different
    # lists. Brush a region to filter the table below it.
    brush = alt.selection_interval()
    plot = (
        alt.Chart(items)
        .mark_circle()
        .encode(
            x=alt.X("reads:Q", scale=alt.Scale(type="symlog"), title="times read"),
            y=alt.Y("cost:Q", scale=alt.Scale(type="symlog"), title="estimated cost (USD)"),
            size=alt.Size("tokens:Q", title="size (tokens)"),
            color=alt.Color("category:N", title="category"),
            tooltip=["item", "cost", "loading", "keeping", "reads", "turns_resident", "tokens"],
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
def _(chart, items, mo):
    selected = chart.value if len(chart.value) else items
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
                "session": row["session_id"][:8],
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


def write_notebook(path: Path = DEFAULT_NOTEBOOK) -> Path:
    """Write the notebook, returning the path written.

    The file is written whole, in one call, so an interrupted run leaves no half-written
    notebook that looks complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTEBOOK_SOURCE, encoding="utf-8")
    return path
