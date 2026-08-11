# ccaudit

Local-first cost observability for Claude Code sessions. It answers *"where did the money
go?"* down to the individual file, tool call, and prompt — including the cost of keeping
context resident across turns.

Every figure it reports is **API-equivalent cost**: imputed from token counts and published
list prices, paired with a share of the session total. It is not a bill.

## Status

Early. Design artifacts only — the specification lives in
[`specs/001-per-file-cost-attribution/`](specs/001-per-file-cost-attribution/), and the cost
model it is built on is [`docs/cost-model.md`](docs/cost-model.md).

## Development

Everything runs through [`uv`](https://docs.astral.sh/uv/):

```sh
uv sync --group dev
uv run ruff format
uv run ruff check
uv run mypy
uv run pytest
```

Engineering standards are the [constitution](.specify/memory/constitution.md); working
conventions are in [`CLAUDE.md`](CLAUDE.md) and [`.claude/rules/`](.claude/rules).

## Privacy

Session transcripts contain file paths, shell commands, and source code. Everything stays on
your machine — nothing is transmitted, and the tool never writes to `~/.claude/`.
