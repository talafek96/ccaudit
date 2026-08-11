# Feature Specification: Per-File Cost Attribution with Carry Cost

**Feature Branch**: `001-per-file-cost-attribution`

**Created**: 2026-08-11

**Status**: Draft

**Input**: User description: "ccaudit — a local-first cost observability tool for Claude Code sessions that attributes spend to individual files and folders, including the cost of keeping content resident in context across turns, and that a team runs repeatedly during development to find and fix what is expensive."

## Overview

**ccaudit** answers one question that no existing tool answers: *for a given Claude Code session, how much did each individual file cost me, and why?*

The "why" is the hard part and the reason the tool must exist. Content that enters a
conversation is paid for **twice**: once when it is loaded, and then again on **every
subsequent turn** for as long as it stays loaded. Measured across a real 23-session corpus,
that second charge — the **carry cost** — is roughly 54% of all spend, against 22% for the
initial load. Any accounting that charges a file only for the moment it was read therefore
explains less than a quarter of the money, and ranks the wrong files as expensive.

The tool exists to settle arguments with evidence and then to keep settling them as the
codebase changes. A number that cannot be reconciled, traced, or reproduced by a skeptic is
worthless for that purpose, so the honesty properties in this spec (reconciliation,
confidence, traceability, plain-language naming) are functional requirements, not polish.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Find out where a session's money went (Priority: P1)

A developer finishes an expensive Claude Code session and wants to know which files, folders,
and categories consumed the spend. They point the tool at the session and get a ranked
breakdown in which every dollar is accounted for — including an explicit line for anything
the tool could not attribute.

**Why this priority**: This is the atomic unit of value. Every other story is built on top of
a trustworthy per-file breakdown of one session; without it, nothing else can be believed.

**Independent Test**: Run the tool against a single recorded session and verify that the
per-file, per-folder, and per-category breakdowns each sum to the session total, that the
unattributed remainder is shown explicitly, and that the ranking is stable across repeat runs
of the same input.

**Acceptance Scenarios**:

1. **Given** a completed session record, **When** the user requests a cost breakdown,
   **Then** the system reports total API-equivalent cost, a per-file ranked breakdown, and an
   explicit unattributed remainder that together sum to the total.
2. **Given** a breakdown has been produced, **When** the user sums every per-file figure plus
   the unattributed remainder, **Then** the result equals the reported session total exactly.
3. **Given** any single reported figure, **When** the user asks how it was derived,
   **Then** the system identifies the source records it came from and the basis used.
4. **Given** the same session is analysed twice, **When** the two results are compared,
   **Then** every figure is identical.
5. **Given** a session where some cost cannot be attributed to any known item, **When** the
   breakdown is produced, **Then** the unattributed portion is reported as its own visible
   entry rather than distributed across the attributable items.

---

### User Story 2 - Understand *why* a file is expensive (Priority: P1)

A developer sees an expensive file at the top of the ranking and needs to know which of two
very different problems they have: a file being **read over and over**, or a file **read once
and then carried for the rest of the session**. The two have opposite remedies, and the total
cost alone cannot distinguish them.

**Why this priority**: The tool's purpose is optimization, not reporting. A ranking without a
cause tells the user something is expensive but not what to do about it, which is the
difference between a report and a tool.

**Independent Test**: Construct two sessions with the same total cost for a file — one from
many repeat loads, one from a single early load carried to the end — and verify the tool
distinguishes them and recommends different remedies.

**Acceptance Scenarios**:

1. **Given** a file in the breakdown, **When** the user inspects it, **Then** the system
   separates its cost into the one-time loading charge and the recurring carrying charge.
2. **Given** a file that was loaded once and remained available for many turns, **When** the
   user inspects it, **Then** the system reports how many turns it remained resident and the
   cost attributable to that residency.
3. **Given** a file loaded repeatedly, **When** the user inspects it, **Then** the system
   reports the number of loads and the cost of each.
4. **Given** two files with equal total cost but different cause profiles, **When** both are
   inspected, **Then** their loading-versus-carrying splits differ measurably.
5. **Given** content left the conversation partway through a session, **When** costs are
   attributed, **Then** the system stops charging carrying cost for that content from that
   point onward.

---

### User Story 3 - Settle the "are our docs expensive?" question (Priority: P2)

A manager believes a large share of spend comes from reading and re-reading `.md` files —
instruction files, skills, and specs. A developer needs to confirm or refute this with numbers
credible enough to show in a meeting, over real sessions rather than one anecdote.

**Why this priority**: This is the specific dispute that motivated the tool and the first
real-world test of whether its output survives scrutiny. It ranks below the first two because
it consumes their output rather than adding new measurement.

**Independent Test**: Run the tool over a multi-session corpus and verify it produces both
halves of the answer — the cost of always-present instruction content and the cost of files
read during work — as separately labelled figures that a non-expert reads correctly.

**Acceptance Scenarios**:

1. **Given** a session, **When** the user requests the instruction-content breakdown, **Then**
   the system reports the cost of each always-present component (instruction files, skills,
   available tool descriptions, base instructions) separately rather than as one bucket.
2. **Given** both breakdowns exist, **When** they are presented together, **Then** the cost of
   always-present content and the cost of files read during work are shown side by side on a
   common scale, so their magnitudes can be compared directly.
3. **Given** a breakdown by file category, **When** the user groups by category, **Then**
   documentation, source code, specs, skills, and tool descriptions are reported as distinct
   categories.
4. **Given** any figure in the comparison, **When** a skeptical reader challenges it, **Then**
   its basis and confidence are visible without rerunning the tool.
5. **Given** the underlying records are known to under-report some injected content, **When**
   results are presented, **Then** that limitation is stated alongside the affected figures.

---

### User Story 4 - Share the finding (Priority: P2)

A developer needs to hand the result to a manager who will not install anything, run anything,
or read a terminal.

**Why this priority**: The evidence is worthless if it cannot leave the machine it was produced
on. Low effort relative to its value, but it depends on Stories 1–3 having produced something
worth sharing.

**Independent Test**: Produce a report, move the file to a machine with no tooling installed,
open it, and confirm every figure and visual renders with no network access.

**Acceptance Scenarios**:

1. **Given** an analysed session, **When** the user requests a report, **Then** the system
   produces a single self-contained file that opens in a browser with no additional software.
2. **Given** a report file, **When** it is opened without network access, **Then** all content
   renders correctly.
3. **Given** a report is opened, **When** the reader looks at any cost figure, **Then** it is
   labelled as an estimate of API-equivalent cost and accompanied by its share of the total.
4. **Given** a user is about to share a report, **When** they choose to redact, **Then**
   file paths are obscured while the cost structure remains intact and readable.

---

### User Story 5 - Know what a change cost (Priority: P3)

A team adds a tool integration, a skill, or a section to an instruction file, and needs to know
whether that change made their sessions more expensive.

**Why this priority**: This is what makes the tool a development companion rather than a
one-time investigation, but it is only meaningful once single-session numbers are trusted.

**Independent Test**: Analyse two sessions of comparable work recorded before and after a
configuration change and verify the tool reports a per-item delta identifying what grew.

**Acceptance Scenarios**:

1. **Given** two analysed sessions, **When** the user compares them, **Then** the system
   reports which items grew, which shrank, and by how much.
2. **Given** many analysed sessions, **When** the user aggregates them, **Then** costs can be
   grouped across a project, a date range, or all sessions.
3. **Given** a series of sessions over time, **When** the user views the trend, **Then**
   changes in always-present overhead are distinguishable from changes in work-driven cost.
4. **Given** a session is analysed more than once, **When** results are stored, **Then**
   the stored history contains one entry per session, not duplicates.

---

### User Story 6 - See what could be saved (Priority: P4)

A developer wants the tool to quantify the opportunity, not just the cost: how much would have
been saved if a specific expensive item had not been carried for so long.

**Why this priority**: The highest-value output for optimization, and the most dependent on
everything else being correct. A wrong counterfactual is worse than none, so it goes last.

**Independent Test**: For a session with a long-resident expensive item, verify the reported
saving equals the difference between the actual attributed cost and a recomputation with that
item's residency shortened.

**Acceptance Scenarios**:

1. **Given** an analysed session, **When** the user asks what could have been saved, **Then**
   the system ranks items by the saving that would have resulted from removing them earlier.
2. **Given** a proposed saving, **When** the user inspects it, **Then** the system states the
   assumption behind it and labels it as a counterfactual estimate, not a measurement.

---

### Edge Cases

- **A session was resumed, forked, or continued after compaction.** Records for the same
  exchange may appear more than once across files. Each exchange must be counted exactly once;
  a session analysed after a resume must not double its reported cost.
- **Content left the conversation without an explicit record.** Some removal happens silently.
  Cost that can no longer be attributed to a known resident item must surface in the
  unattributed remainder rather than being spread across surviving items.
- **A tool result is an image rather than text.** Image payloads are the dominant contributor
  to tool-result volume and are drastically mis-measured by length-based estimation. They must
  be measured by a method appropriate to their type, or excluded and declared.
- **A session used subagents.** Work performed by a subagent must roll up to the parent that
  spawned it, and must not be double-counted at both levels.
- **A file was read, modified, and read again.** Each load is a distinct injection with its own
  cost and its own residency span.
- **The same file path appears in multiple projects.** Attribution must not merge distinct
  files that happen to share a path.
- **A session is still in progress.** Partial records must produce a partial result labelled as
  such, not a crash and not a silently truncated total.
- **Records are malformed or from an unrecognised version.** Unparseable records are counted
  and surfaced in the run summary rather than skipped silently.
- **Recorded content was never actually charged** (or vice versa). Where the underlying records
  and the reported totals disagree, the discrepancy is reported rather than reconciled by
  adjusting one to match the other.
- **A session has no attributable file activity at all.** The tool reports a valid result
  showing cost dominated by always-present content, not an empty or error state.

## Requirements *(mandatory)*

### Functional Requirements

**Attribution**

- **FR-001**: System MUST attribute cost to individual context items — files, always-present
  instruction content, tool descriptions, skills, and conversation content — for a single
  session.
- **FR-002**: System MUST decompose each item's cost into a **direct** component (the one-time
  charge for the content entering the conversation) and a **carry** component (the recurring
  charge for each turn it remains available).
- **FR-003**: System MUST determine, for each turn of a session, which items were available in
  the conversation and their relative weight, and MUST use that to compute carry cost.
- **FR-004**: System MUST stop accruing carry cost for an item once that item leaves the
  conversation.
- **FR-005**: System MUST attribute generated-output cost to the exchange that produced it and
  MUST NOT attribute it to any file.
- **FR-006**: System MUST support more than one policy for dividing shared carry cost among
  concurrently resident items, selectable by configuration, with a documented default.
- **FR-007**: System MUST report per-item results grouped by file, folder, file extension, and
  category, at each level of the folder hierarchy.
- **FR-008**: System MUST distinguish, for each item, cost caused by repeated loading from cost
  caused by prolonged residency.
- **FR-009**: System MUST roll subagent activity up to the parent exchange that spawned it,
  counting each unit of work exactly once.

**Honesty and integrity**

- **FR-010**: System MUST label every monetary figure as an estimate of API-equivalent cost and
  MUST NOT present any figure as an amount billed.
- **FR-011**: System MUST accompany every absolute monetary figure with its share of the
  relevant total.
- **FR-012**: System MUST ensure every breakdown sums to its stated total, with any difference
  reported as an explicit unattributed entry.
- **FR-013**: System MUST NOT distribute unattributed cost across attributable items.
- **FR-014**: System MUST record, for every reported figure, the basis on which it was derived
  and a confidence level, and MUST make both visible to the reader.
- **FR-015**: System MUST make every reported figure traceable to the source records that
  produced it, without rerunning the analysis.
- **FR-016**: System MUST present a plain-language name for every cost component alongside its
  technical term, drawn from a single authoritative definition.
- **FR-017**: System MUST produce identical results for identical inputs.
- **FR-018**: System MUST state known limitations of the underlying records alongside the
  figures those limitations affect.
- **FR-019**: System MUST decline to report a figure it cannot support, rather than estimating
  one, and MUST show the gap.

**Data handling**

- **FR-020**: System MUST treat all user session records as read-only and MUST NOT modify,
  move, or delete them.
- **FR-021**: System MUST count each recorded exchange exactly once across resumed, forked, and
  compacted sessions.
- **FR-022**: System MUST recognise every mechanism by which content enters the conversation,
  including mechanisms other than direct file reads.
- **FR-023**: System MUST use recorded authoritative measurements where the records provide
  them, in preference to estimation, and MUST mark estimated values as such.
- **FR-024**: System MUST measure non-text content by a method appropriate to its type, or
  exclude it and declare the exclusion.
- **FR-025**: System MUST use recorded evidence of what survived a conversation compaction to
  determine what remained resident afterwards.
- **FR-026**: System MUST reconcile its computed totals against any independent totals present
  in the records, and MUST report disagreements.
- **FR-027**: System MUST count and surface unparseable or unrecognised records in the run
  summary.
- **FR-028**: System MUST record the version of the producing tool alongside ingested data and
  MUST make version-spanning comparisons identifiable as such.
- **FR-029**: System MUST function without any credential, key, or account beyond the user's
  existing local installation.
- **FR-030**: System MUST NOT transmit any data off the machine.
- **FR-031**: System MUST operate correctly when optional richer data sources are unavailable,
  degrading to what the primary records support and declaring the reduced confidence.

**Presentation**

- **FR-032**: System MUST produce a single self-contained report file that opens without
  additional software or network access.
- **FR-033**: System MUST produce a terminal summary sufficient to answer "what was most
  expensive and why" without opening the report.
- **FR-034**: System MUST provide a hierarchical view of cost over the folder tree, navigable
  by drilling into folders, switching between an item's own cost and its cost including
  everything it caused to be loaded.
- **FR-035**: System MUST provide a view showing, per item, the split between loading cost and
  carrying cost.
- **FR-036**: System MUST provide a view showing each item's residency over the course of the
  session, so prolonged residency is visible at a glance.
- **FR-037**: System MUST provide a view comparing always-present content against
  work-driven file reads on a common scale.
- **FR-038**: System MUST provide a ranked tabular view supporting sorting by each reported
  measure.
- **FR-039**: System MUST show cost accumulating over the course of the session, with
  conversation-compaction events marked.
- **FR-040**: System MUST include the unattributed remainder as a visible element in every view
  that presents parts of a whole.
- **FR-041**: System MUST render legibly in both light and dark presentation modes.
- **FR-042**: System MUST convey every distinction by some means in addition to color.
- **FR-043**: System MUST offer a mode that obscures file paths while preserving the cost
  structure, for sharing outside the team.

**Persistence and comparison**

- **FR-044**: System MUST persist analysis results locally so that sessions can be compared
  without re-analysis.
- **FR-045**: System MUST support aggregating results across a project, a date range, or all
  recorded sessions.
- **FR-046**: System MUST support comparing two analysed sessions and reporting per-item
  deltas.
- **FR-047**: System MUST be safely re-runnable, producing no duplicate stored results when run
  repeatedly over the same records.

### Key Entities

- **Session**: One recorded Claude Code conversation. Has a total cost, a project, a time
  range, an ordered sequence of turns, and a producing-tool version.
- **Turn**: One exchange within a session. Carries the token counts that generate cost, and
  the set of items resident at that point.
- **Context Item**: Anything occupying space in the conversation and therefore incurring cost.
  Files are the primary kind; always-present instruction content, tool descriptions, skills,
  and conversation content are others. Has an identity, a size, and one or more residency
  spans.
- **Injection**: One event placing an item into the conversation. Has a cause (a file read, a
  skill activation, an attachment, the session start), a turn, and a size. Origin of direct
  cost.
- **Residency Span**: The interval during which an item remained available, from injection to
  departure. Origin of carry cost.
- **Cost Component**: One of the four ways cost is incurred, each with a technical term, a
  mandatory plain-language name, and a price.
- **Attribution**: The assignment of an amount of cost to a context item, carrying its basis,
  its confidence, and a reference to the source records supporting it.
- **Category**: A classification of a context item (documentation, source, spec, skill, tool
  description, other) used for grouping.
- **Analysis Run**: One execution over a set of sessions, recording when it ran, what it
  covered, which policies were in effect, and what it could not parse.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every analysed session, the sum of all per-item attributions plus the
  unattributed remainder equals the reported session total exactly, with zero tolerance.
- **SC-002**: The unattributed remainder is under 15% of total session cost for a typical
  session, and is always displayed regardless of size.
- **SC-003**: Reported session totals agree with independently established totals for the same
  sessions to within 1%, or the discrepancy is reported.
- **SC-004**: A user can identify the single most expensive file in a session, and the reason
  it is expensive, within 60 seconds of starting the tool on an unfamiliar session.
- **SC-005**: Analysing a typical single session completes in under 30 seconds on a developer
  laptop, with no service running beforehand.
- **SC-006**: Analysing a full local corpus of roughly 25 sessions completes in under 5 minutes.
- **SC-007**: A reader who has never used the tool can correctly state what each of the four
  cost components means, from the report alone, without asking a follow-up question.
- **SC-008**: Given the tool's output, a skeptical reader can trace any single figure back to
  the records that produced it without rerunning the tool.
- **SC-009**: The same input produces byte-identical figures across runs and across machines.
- **SC-010**: Ranking files by total attributed cost produces a materially different top-10
  ordering than ranking by read count alone, on at least one real session — demonstrating that
  carry cost changes the answer and the tool is measuring something new.
- **SC-011**: The tool runs on a machine with no configuration beyond a Claude Code
  installation, requiring no credential, service, or network access.
- **SC-012**: A report file opens and renders completely on a machine with no project tooling
  installed and no network connection.
- **SC-013**: Re-running the tool over unchanged records changes no stored result and creates
  no duplicate entries.
- **SC-014**: For a session containing images, resumes, subagents, and a compaction, the tool
  produces a complete result with no crash and with every affected limitation declared.

## Assumptions

**Scope**

- **Single-session analysis is the v1 boundary.** Stories 1–4 (per-file attribution, cause
  analysis, the instruction-versus-reads comparison, and the shareable report) constitute the
  first release. Cross-session aggregation and comparison (Story 5) and the counterfactual
  panel (Story 6) follow once single-session numbers are demonstrably trustworthy. Rationale:
  the value and the risk both live in the per-file numbers being correct; aggregation is
  mechanical once they are, and worthless if they are not. Requirements FR-044 to FR-047 are
  specified now so the storage design does not have to be revisited, but only FR-044 and
  FR-047 are required for v1.
- Analysis is retrospective, over completed session records. Live monitoring during a session
  is out of scope.
- The tool analyses one machine's local records. Combining records across a team's machines is
  out of scope.

**Cost model**

- Monetary figures are imputed from token counts and published list prices. On enterprise
  seats, usage inside the allowance is not itemised in dollars, so no true billed figure is
  available locally. Enterprise pricing is understood to track list pricing closely enough for
  the estimate to be a useful proxy, which is why shares of total are reported alongside every
  absolute figure — the share survives pricing error, the absolute does not.
- The default policy for dividing shared carry cost is proportional to each resident item's
  size. It is chosen for auditability: it is explainable in one sentence and a disputant can
  recompute it by hand. An exclusive-attribution alternative is supported for users who prefer
  it (FR-006).
- Prices live in a single editable configuration so they can be corrected without code changes.

**Data**

- The local session records are the primary source and are assumed to remain broadly stable in
  shape. Their format is undocumented and version-dependent, so a producing-version stamp is
  recorded with all ingested data (FR-028) and unrecognised records are surfaced rather than
  assumed benign (FR-027).
- The records are known to omit some injected instruction content. This is why an explicit
  unattributed remainder is mandatory rather than a nicety — some of the disputed cost is
  provably not in the data, and pretending otherwise would misattribute it.
- Some content leaves the conversation with no recorded marker. That portion is a named
  residual, not an estimate.
- Optional richer telemetry may be unavailable on managed enterprise installations. The tool
  therefore treats it as an enhancement and never a requirement (FR-031).

**Environment and users**

- Users are developers with a working local Claude Code installation; report *readers* may be
  non-technical and are assumed to have only a browser.
- Reports are shared deliberately and by hand. Path redaction is available but off by default,
  since the common case is local use where paths are the most useful identifier (FR-043).
- Single-user, single-machine operation. No multi-user access control is required.

## Dependencies

- A local Claude Code installation that has produced session records. No account, credential,
  or network access is required beyond that.
- The corpus of local session records on the analysing machine, which the tool only reads.

## Out of Scope

- Replacing, wrapping, or proxying Claude Code.
- Real-time monitoring, background daemons, or long-running services.
- Organisation-wide telemetry collection or centralised reporting infrastructure.
- Modifying, moving, pruning, or optimising the user's session records or context.
- Automatically acting on its own recommendations.
- Reporting true billed amounts, which are not available locally.
