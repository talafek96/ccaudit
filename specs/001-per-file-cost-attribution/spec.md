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

### User Story 3 - Run it without ceremony (Priority: P1)

A developer wants an answer, not a setup project. They run one command with no arguments and
get the breakdown for the session they just finished. Later they want the same answer without
leaving Claude Code, and eventually they want it to accumulate on its own so that by the time
someone asks "did that change make things worse?", the history is already there.

**Why this priority**: A tool that is a chore to run gets run once, during the argument, and
never again — which forfeits every ongoing-optimization outcome this product exists for. The
zero-argument default and the in-editor path are what make it a companion rather than an
investigation.

**Independent Test**: On a machine with the tool never previously run, execute it with no
arguments in a project directory and confirm it produces a correct breakdown of the most recent
session with no configuration, no account, and no prior setup step.

**Acceptance Scenarios**:

1. **Given** a developer in a project directory that has session history, **When** they run the
   tool with no arguments, **Then** it analyses the most recent session for that project and
   reports the breakdown, without requiring configuration.
2. **Given** the tool has never been run on this machine, **When** it is run for the first time,
   **Then** it requires no setup step, no account, and no credential.
3. **Given** a developer working inside Claude Code, **When** they invoke the tool from within
   their session, **Then** they receive the breakdown for that same in-progress session without
   switching to a terminal.
4. **Given** a developer asks their assistant a question about session cost in natural language,
   **When** the assistant has the integration available, **Then** the assistant can produce the
   answer from real measured data rather than estimating.
5. **Given** automatic capture is enabled, **When** a session ends, **Then** its analysis is
   recorded without the user taking any action.
6. **Given** automatic capture is enabled, **When** a session ends, **Then** the user's workflow
   is not blocked, delayed, or interrupted by the analysis.
7. **Given** the integration is installed, **When** the user measures its own footprint,
   **Then** the cost it adds to every session is reported and is a negligible share of session
   cost.
8. **Given** the integration is uninstalled, **When** the user runs sessions afterwards,
   **Then** no residue of the tool remains in their sessions and previously recorded results
   are retained.

---

### User Story 4 - Settle the "are our docs expensive?" question (Priority: P2)

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

### User Story 5 - Share the finding (Priority: P2)

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

### User Story 6 - Analyse many sessions at once (Priority: P2)

A developer wants the accumulated picture, not one session's worth: which files have cost the
most across a week, a project, or everything recorded. They pick a set of sessions, see the
combined total, and can drop a session that skews the picture — an unusual spike, a session on
an unrelated branch — and see the numbers recompute without it.

**Why this priority**: One session is an anecdote. The manager-facing argument and the
"which files are chronically expensive" question both need accumulation across sessions.
It ranks below the single-session stories only because it consumes their output.

**Independent Test**: Select several past sessions, verify the combined per-file totals equal
the sum of the individual per-session figures, then exclude one session and verify the totals
drop by exactly that session's contribution.

**Acceptance Scenarios**:

1. **Given** local records containing many past sessions, **When** the user lists what is
   available, **Then** the system shows the sessions it can analyse, with enough detail to
   identify them.
2. **Given** a set of selected sessions, **When** the user analyses them together, **Then** the
   system reports a combined total and per-item attribution accumulated across all of them.
3. **Given** a multi-session result, **When** the user excludes one session, **Then** the
   totals recompute to exclude exactly that session's contribution, without re-reading the
   original records.
4. **Given** a multi-session result, **When** the user inspects any item, **Then** the system
   shows both its accumulated cost and its cost in each contributing session.
5. **Given** a multi-session result, **When** the user reads it, **Then** it states which
   sessions are included and how many were excluded.
6. **Given** sessions spanning different versions of the producing tool, **When** they are
   aggregated, **Then** the result identifies that it spans versions.

---

### User Story 7 - Watch a session as it runs (Priority: P2)

A developer mid-session suspects something has gone expensive — a large file read, a runaway
loop — and wants to see the cost breakdown now, without ending the session.

**Why this priority**: Catching a problem while it is still accruing is worth more than
diagnosing it afterwards, and it is what makes the tool part of the working loop. It ranks
below single-session analysis because it is the same measurement applied to partial data.

**Independent Test**: With a session in progress, run the tool and verify it produces a
breakdown of activity so far, clearly labelled as provisional.

**Acceptance Scenarios**:

1. **Given** a session in progress, **When** the user analyses it, **Then** the system reports
   the breakdown of activity recorded so far.
2. **Given** an in-progress analysis, **When** the result is presented, **Then** it is labelled
   provisional and notes that the most recent activity may not yet be included.
3. **Given** an in-progress session, **When** the user enables the refreshing mode, **Then**
   the breakdown updates as the session continues without re-invocation.
4. **Given** the same session is analysed again after it ends, **When** the results are
   compared, **Then** the final result supersedes the provisional one rather than being added
   to it.

---

### User Story 8 - Know what a change cost (Priority: P3)

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

### User Story 9 - See what could be saved (Priority: P4)

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

**Invocation and integration**

- **FR-048**: System MUST produce a useful result when invoked with no arguments, defaulting to
  the most recent session of the project in the current working directory.
- **FR-049**: System MUST be runnable without a prior install step, and MUST also support being
  installed for repeat use.
- **FR-050**: System MUST require no configuration file, account, credential, or service before
  first use, and MUST create any state it needs on demand.
- **FR-051**: System MUST be invocable from within a Claude Code session and, when so invoked,
  MUST default to analysing that same session, including while it is still in progress.
- **FR-052**: System MUST expose its capability such that an assistant can invoke it in response
  to a natural-language question about session cost, and answer from measured data.
- **FR-053**: System MUST offer an opt-in mode that records a session's analysis automatically
  when the session ends, requiring no user action.
- **FR-054**: Automatic recording MUST NOT block, delay, or interrupt the user's session, and
  MUST fail silently to a log rather than surfacing errors into the user's workflow.
- **FR-055**: Installing the in-editor integration MUST NOT increase the size of the user's
  conversations. Anything the integration adds to a conversation MUST appear only at the moment
  the user invokes it, and MUST NOT be present in conversations where it is never used.
- **FR-056**: System MUST be able to measure and report its own contribution to session cost.
- **FR-057**: System MUST be removable such that no residue remains in the user's sessions,
  while previously recorded results are retained.
- **FR-058**: System MUST be installable and updatable through the mechanism Claude Code
  provides for distributing such integrations, without manual file copying.

**Session selection and multi-session analysis**

- **FR-059**: System MUST analyse any past session, not only recent ones, for as far back as
  local records exist.
- **FR-060**: System MUST let the user choose which sessions to analyse — by project, by date
  range, by explicit identifier, or by selecting from a browsable list of available sessions.
- **FR-061**: System MUST support analysing several sessions together, reporting their combined
  total and per-item attribution accumulated across all of them.
- **FR-062**: System MUST let the user include or exclude individual sessions from a
  multi-session analysis, and MUST recompute results from stored data without re-reading the
  original records.
- **FR-063**: System MUST state, for any multi-session result, exactly which sessions are
  included and how many were excluded.
- **FR-064**: System MUST, for an item appearing in several sessions, report both its
  accumulated total and its contribution per session.
- **FR-065**: System MUST keep sessions distinguishable after aggregation, so that a
  multi-session figure can be decomposed back to the sessions that produced it.

**In-progress sessions**

- **FR-066**: System MUST analyse a session that is still running, covering activity recorded
  so far.
- **FR-067**: System MUST label results for an in-progress session as provisional and state
  that the most recent activity may not yet be included.
- **FR-068**: System MUST offer a mode that refreshes an in-progress session's analysis as the
  session continues, without the user re-invoking it.
- **FR-069**: System MUST NOT require a session to have ended before any of its cost can be
  attributed.

**Presentation surfaces**

- **FR-070**: System MUST provide a rich terminal presentation — formatted tables, proportion
  bars, and colour — that answers "what was most expensive and why" without leaving the
  terminal.
- **FR-071**: Terminal output MUST degrade to plain, parseable text when not attached to an
  interactive terminal, so it can be piped or captured.
- **FR-072**: System MUST provide an interactive local browser interface supporting drill-down,
  sorting, filtering, session selection, and switching between views, offering materially
  richer exploration than the terminal presentation.
- **FR-073**: The browser interface MUST be started by a single command, MUST be reachable only
  from the local machine, MUST make no external network requests, and MUST shut down cleanly on
  request.
- **FR-074**: The browser interface MUST NOT be required for any core result; every figure it
  presents MUST also be obtainable from the terminal.
- **FR-075**: System MUST distinguish the interactive interface from the exportable report
  (FR-032): the former is for exploring, the latter is a frozen artifact for sharing.

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
- **SC-015**: A first-time user goes from "never heard of it" to a correct breakdown of their
  most recent session in a single command and under 2 minutes, including any download, with no
  configuration and no account.
- **SC-016**: The in-editor integration is installed in under 2 minutes by a user who has not
  installed one before, using only the mechanism Claude Code already provides.
- **SC-017**: The tool's own always-resident contribution to a session is under 0.5% of that
  session's total cost, and the tool reports this figure about itself.
- **SC-018**: With automatic recording enabled, the user perceives no added delay at session
  end, and a failure of the recording never surfaces as an error in their session.
- **SC-019**: After the integration is removed, a subsequent session contains no trace of it,
  and every previously recorded result remains available.
- **SC-020**: Combined per-item totals across a set of sessions equal the sum of those items'
  per-session figures exactly; excluding a session reduces the totals by exactly that session's
  contribution.
- **SC-021**: Changing which sessions are included in an analysis produces updated results in
  under 2 seconds for a corpus of 100 sessions, without re-reading the original records.
- **SC-022**: A user can analyse a session from months earlier, provided its records still
  exist locally, with no loss of detail relative to a recent one.
- **SC-023**: Analysing a session that is still running returns a breakdown of activity so far,
  labelled provisional, and never reports a figure that later proves to have been an
  over-count.
- **SC-024**: Every figure available in the interactive interface is also obtainable from the
  terminal, so no capability is exclusive to the browser.
- **SC-025**: The interactive interface starts from a single command in under 5 seconds, serves
  only the local machine, makes no external requests, and leaves nothing running after it is
  closed.

## Assumptions

**Scope**

- **v1 is Stories 1–7**: per-file attribution, cause analysis, frictionless invocation, the
  instruction-versus-reads comparison, the shareable report, multi-session accumulation with
  include/exclude, and in-progress analysis. **Story 8** (before/after comparison of a
  configuration change) and **Story 9** (counterfactual savings) follow. Rationale: the value
  and the risk both live in the per-file numbers being correct. Multi-session accumulation is
  arithmetic over those numbers and is required for the manager-facing argument, so it is in.
  Stories 8 and 9 are *interpretations* of the numbers — a delta needs two runs to be
  comparable, and a counterfactual asserts what would have happened — so both are deferred
  until the underlying figures are demonstrably trustworthy. A wrong counterfactual is worse
  than none.
- Analysis is retrospective over recorded activity, including the activity recorded so far in a
  running session. It is not a live instrument reading the conversation as it is constructed:
  the tool reads what has been written down, which lags the live conversation slightly.
- The tool analyses one machine's local records. Combining records across a team's machines is
  out of scope.
- Session records persist locally for as long as Claude Code retains them; the tool can analyse
  any session still on disk, however old. It does not archive records to extend their life
  (that would violate the read-only treatment of user data), though its own stored results
  outlive the records they were derived from.

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

**Invocation**

- **The tool must not become the thing it measures.** This constrains the integration design
  more than any preference does. An integration that registers persistent capabilities adds its
  descriptions to the resident context of *every* session, permanently — and the research
  underlying this spec found that exactly this category of always-resident tool description is
  the single largest block of resident context, roughly fifty times the size of a project's
  instruction file. A cost-observability tool that inflated that block would corrupt the
  baseline it exists to measure and would show up in its own reports. Hence FR-055: any
  in-editor integration consumes context only when actively invoked, and FR-056 requires the
  tool to measure and disclose its own footprint rather than assume it is negligible.
- Three invocation paths are assumed, in decreasing order of expected use: a terminal command
  (the primary and only mandatory one), invocation from within a Claude Code session, and
  opt-in automatic recording at session end. Only the first is required for the tool to be
  useful; the others make it habitual.
- Automatic recording is opt-in rather than default, because writing to a user's environment
  without being asked is a decision that belongs to the user (consistent with how this project
  treats the user's own directories elsewhere).
- **Automatic recording must be near-instant, not merely fast.** Session-end handlers run
  inside a shared time budget, and a handler supplied by an installed integration cannot raise
  that budget for itself. The assumption is therefore that automatic recording records *that a
  session needs analysing* and returns immediately, with the analysis itself happening on the
  next invocation — never that the analysis runs inside the session-end handler. This also
  keeps FR-054 satisfiable: a slow analysis can never delay the user.
- Automatic recording is a convenience, not the system of record. Sessions that end in ways
  that do not trigger a handler (a crash, a closed terminal, a killed process) are still fully
  analysable afterwards from their records, so no data is lost by relying on it — or by not
  using it at all.

**Presentation surfaces**

- Three distinct surfaces are assumed, and they are not substitutes for one another: the
  **terminal** presentation (fast, always available, answers the question in place), the
  **interactive local interface** (exploration — drill-down, filtering, session selection), and
  the **exportable report** (a frozen artifact for someone who will not run anything). The
  terminal surface is mandatory and complete on its own (FR-074); the other two are what make
  the tool pleasant and shareable respectively.
- The interactive interface is assumed to be started on demand and shut down when finished, not
  left running. This keeps it consistent with the no-daemon constraint: it is a command that
  happens to render in a browser, not a service.

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
- Background daemons or long-running services. The interactive interface is started on demand
  and stopped when finished; analysing an in-progress session is a repeated read of what has
  been recorded, not a resident monitor.
- Organisation-wide telemetry collection or centralised reporting infrastructure.
- Modifying, moving, pruning, or optimising the user's session records or context.
- Automatically acting on its own recommendations.
- Reporting true billed amounts, which are not available locally.
- Registering persistent always-resident capabilities in the user's sessions (see Assumptions,
  *Invocation*).
