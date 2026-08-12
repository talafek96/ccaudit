"""How an item is classified and named for a reader — one authority for both (Principle IX).

Categories are what the "are our docs expensive?" question is actually asked in (US4,
FR-007). They are deliberately few: a category a reader has to look up is a defect.

The same rule governs an item's *name*. Most items are files and name themselves, but a few
are content Claude Code injects that has no path — the skill listing, the tool schemas, the
subagent listing. Their record keys (`skill_listing`) leaked into the report as the displayed
name, which is jargon only the author understands and therefore a defect under Principle X.
Each carries a plain name and a sentence saying what it actually is, defined once, here.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath

CATEGORIES: tuple[str, ...] = ("docs", "source", "spec", "skill", "schema", "other")

# What a merged row's category reads as when its members disagree. Not a category — it is the
# absence of one, and saying so beats naming whichever member happened to arrive first.
MIXED_CATEGORY = "(mixed)"

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "docs": "Documentation and instruction files — READMEs, CLAUDE.md, guides, notes.",
    "source": "Source code and its tests.",
    "spec": "Specifications, plans, and design artifacts.",
    "skill": "Skill definitions loaded into the conversation.",
    "schema": "Tool and MCP descriptions the model is shown.",
    "other": "Everything else — data, config, lockfiles, binaries.",
}

# What each tag on a row means, in a sentence. A tag is a compression of a finding — "too
# small to cache on claude-opus-5" is four facts in six words — and a compression the reader
# cannot expand is jargon (Principle X). Category tags take their text from
# CATEGORY_DESCRIPTIONS above; these are the rest. `{model}` and `{plugin}` are filled at the
# call site with the value the tag is about.
TAG_DESCRIPTIONS: dict[str, str] = {
    "mixed": (
        "This row merges items of more than one category, so no single category names it. "
        "Group by item to see the categories separately."
    ),
    "uncacheable": (
        "This content is smaller than {model}'s minimum cacheable block, so it could not be "
        "written to the cache and was charged at the full input rate on every turn it stayed "
        "in context — about ten times the rate cached content is carried at."
    ),
    "plugin": (
        "This skill arrives with the installed plugin {plugin} rather than being one you "
        "wrote. Its cost is yours, but its content is not yours to edit."
    ),
    "running": (
        "This session had not ended when it was read, so the most recent turns may be missing "
        "and its figures may rise."
    ),
}


def tag_description(tag: str, **values: str) -> str:
    """The sentence for a tag, raising on an unknown one (Principle I)."""
    if tag in CATEGORY_DESCRIPTIONS:
        return CATEGORY_DESCRIPTIONS[tag]
    try:
        return TAG_DESCRIPTIONS[tag].format(**values)
    except KeyError:
        raise KeyError(
            f"unknown tag {tag!r}; known: {sorted(set(TAG_DESCRIPTIONS) | set(CATEGORY_DESCRIPTIONS))}. "
            f"Add it to config/categories.py, not at the call site."
        ) from None


# Injected content that has no path. The plain name is what the reader sees; the record key is
# kept as the secondary label, so anyone matching the report against a transcript still can.
INJECTED_ITEM_NAMES: dict[str, str] = {
    "skill_listing": "Skill listing",
    "deferred_tools_delta": "Tool schemas",
    "agent_listing_delta": "Subagent listing",
}

INJECTED_ITEM_DESCRIPTIONS: dict[str, str] = {
    "skill_listing": (
        "The menu of skills Claude can invoke — every available skill's name and one-line "
        "description, injected into the conversation and then carried on every turn after it. "
        "It is not the skills themselves: a skill's own text is only loaded once it is used."
    ),
    "deferred_tools_delta": (
        "The definitions of the tools Claude can call — each tool's name, description, and "
        "parameters, including every tool an MCP server contributes."
    ),
    "agent_listing_delta": (
        "The menu of subagent types Claude can spawn, with each one's description."
    ),
}


def injected_name(identity: str) -> str | None:
    """The plain name for a pathless injected item, or ``None`` if the identity names a file."""
    return INJECTED_ITEM_NAMES.get(identity)


_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".lua",
        ".r",
        ".m",
        ".mm",
        ".ex",
        ".exs",
        ".clj",
        ".hs",
        ".ml",
        ".vue",
        ".svelte",
    }
)

_DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc", ".org"})

_INSTRUCTION_NAMES: frozenset[str] = frozenset({"claude.md", "agents.md"})


@dataclass(frozen=True)
class Categorized:
    """A category plus the rule that produced it, so `--explain` can show the reasoning."""

    category: str
    reason: str


def categorize(identity: str, kind: str = "file") -> Categorized:
    """Classify a context item, returning both the category and why.

    ``kind`` wins over the path when it is unambiguous: a tool schema is a schema no matter
    what it is called. Otherwise the path decides, most specific rule first.
    """
    if kind in ("tool_schema", "mcp_schema"):
        return Categorized("schema", f"item kind is {kind}")
    if kind == "skill":
        return Categorized("skill", "item kind is skill")
    if kind == "system_prompt":
        return Categorized("docs", "the base instructions are instruction content")
    if kind == "conversation":
        return Categorized("other", "conversation content is not a file")

    path = PurePosixPath(identity)
    name = path.name.lower()
    parts = [p.lower() for p in path.parts]
    suffix = path.suffix.lower()

    if name in _INSTRUCTION_NAMES:
        return Categorized("docs", f"{path.name} is an instruction file")
    if "skills" in parts or name == "skill.md":
        return Categorized("skill", "path is inside a skills directory")
    if "specs" in parts or ".specify" in parts:
        return Categorized("spec", "path is inside a specs directory")
    if suffix in _SOURCE_SUFFIXES:
        return Categorized("source", f"{suffix} is a source extension")
    if suffix in _DOC_SUFFIXES:
        return Categorized("docs", f"{suffix} is a documentation extension")
    return Categorized("other", "no category rule matched")
