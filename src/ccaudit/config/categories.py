"""File-category rules — the single authoritative classification (Principle IX).

Categories are what the "are our docs expensive?" question is actually asked in (US4,
FR-007). They are deliberately few: a category a reader has to look up is a defect.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath

CATEGORIES: tuple[str, ...] = ("docs", "source", "spec", "skill", "schema", "other")

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "docs": "Documentation and instruction files — READMEs, CLAUDE.md, guides, notes.",
    "source": "Source code and its tests.",
    "spec": "Specifications, plans, and design artifacts.",
    "skill": "Skill definitions loaded into the conversation.",
    "schema": "Tool and MCP descriptions the model is shown.",
    "other": "Everything else — data, config, lockfiles, binaries.",
}

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
