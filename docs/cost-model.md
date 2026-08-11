# The cost model

**Durable reference. Read before writing or reviewing anything that produces a number.**

This document is tracked in git deliberately — `PITFALLS.md` and `docs/research/` are local-only,
so this is where the load-bearing cost mechanics live for anyone who clones the repo or takes over
the project. Where the two disagree, this file wins.

---

## 1. The four components and what they actually cost

Every request bills four token classes. Their *rates* differ by an order of magnitude, so the
class matters more than the count.

| Technical term | Plain-language name | Rate vs. base input |
|---|---|---|
| `input_tokens` | Your new typing | **1×** (full price) |
| `cache_creation_input_tokens` | Loading into context | **1.25×** at 5-minute TTL, **2×** at 1-hour TTL |
| `cache_read_input_tokens` | Keeping context loaded | **0.1×** |
| `output_tokens` | What Claude wrote back | output rate |

Two consequences that are easy to get wrong:

- **A cache write is not one price.** The TTL doubles it. Break-even differs accordingly: two
  requests at the 5-minute TTL (1.25 + 0.1 vs. 2.0), but at least three at the 1-hour TTL
  (2.0 + 0.2 vs. 3.0). Never apply a single write multiplier across a session.
- **`input_tokens` is the uncached remainder, not the prompt size.** Conversation size is
  `input + cache_creation + cache_read`. A session showing 4K `input_tokens` after hours of work
  is not a small session.

---

## 2. Content can be resident and still not be cached

There is a **minimum cacheable prefix**. Below it, content silently fails to cache — no error,
`cache_creation_input_tokens: 0` — and is billed as **fresh input at full price on every turn**.

The threshold is model-dependent and **does not decrease monotonically across generations**:

| Model | Minimum |
|---|---:|
| Opus 5 | 512 |
| Opus 4.8 / Sonnet 5 / Sonnet 4.6 | 1024 |
| Opus 4.7 | 2048 |
| Opus 4.6 / Opus 4.5 / Haiku 4.5 | 4096 |

**Why this is central to this product, not a footnote.** A measured `CLAUDE.md` of 984 tokens
caches on Opus 5 and does not on Opus 4.6 — the *same file* costs roughly **10× more per turn** on
one model than the other, because it moves from the 0.1× lane to the 1× lane. Any model that
assumes "resident ⇒ cheap" misprices exactly the files this tool exists to price, and does so in
the direction that would let a wrong conclusion survive.

Resolve the threshold from the model recorded **on each request**. A corpus spanning models spans
thresholds.

---

## 3. Invalidation is tiered — and it decides who gets blamed

Render order is `tools` → `system` → `messages`. Caching is a **prefix match**: any byte change
invalidates everything *after* it. That yields three tiers:

| Change | tools | system | messages |
|---|:--:|:--:|:--:|
| Tool definitions added / removed / reordered | ✗ | ✗ | ✗ |
| Model switch | ✗ | ✗ | ✗ |
| System prompt content (incl. instruction files) | ✓ | ✗ | ✗ |
| `tool_choice`, images, thinking toggle | ✓ | ✓ | ✗ |
| Message content | ✓ | ✓ | ✗ |

Instruction files live in the `system` tier, *after* tool schemas. So **adding one MCP server
mid-session forces every instruction file to be re-written** at 1.25–2×.

A naive tool reports *"CLAUDE.md got expensive."* The honest finding is *"adding that server cost
$X in forced re-writes."* Attribute a forced reload to **the change that caused it**, never to the
content that was re-loaded as a consequence (FR-081).

---

## 4. "Cache miss" has four distinct causes

Collapsing them destroys the tool's advice, because each has a different fix:

| Cause | What happened | Fix it implies |
|---|---|---|
| **Evicted** | Content left the conversation (compaction, `/clear`) | Nothing — expected |
| **Invalidated** | Something earlier in the prefix changed | Stabilize the prefix; stop changing tools mid-session |
| **Never eligible** | Below the model's minimum cacheable prefix | Consolidate small files, or accept full rate |
| **Lookback miss** | A breakpoint walks back at most **20 content blocks**; a turn adding more (routine in agentic loops) finds nothing | Structural — not the user's fault, but explains a confusing bill |

Also: max **4** breakpoints per request, and a cache entry is only readable once the first response
*begins streaming* — so parallel requests with identical prefixes all pay full price.

---

## 5. How attribution survives all of this

The complications above are why the tool is hard, not why it's impossible. Five moves make the
requirements achievable:

### 5.1 Observe, don't predict

The single most important design decision. **We never model what the cache *should* have done.**
Every turn's transcript records what was *actually charged* — `input_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, and the model. Thresholds, TTLs, and
invalidation tiers are used to **explain** an observed number, never to **derive** one.

This is what keeps §2 and §3 from being fatal. We don't need to predict whether a 984-token file
cached; we read whether it did, and use the threshold table to say *why*.

### 5.2 Classify each resident item's cost stream, then attribute within the class

Per turn, each resident item sits in exactly one lane:

- **cached** — contributes to `cache_read`, priced at 0.1×
- **uncached (sub-threshold)** — contributes to `input_tokens`, priced at 1× *every turn*
- **(re)loading this turn** — contributes to `cache_creation`, priced at 1.25× or 2×

Attribution then runs **within** each lane against the items in that lane, rather than splitting
one undifferentiated pool. Sub-threshold content is actually the *easiest* to attribute: it
recurs identically every turn at full price.

### 5.3 Reconcile at session level, never assert turn-level exactness

Measured on real transcripts: the ratio of a turn's `cache_creation` to the estimated size of the
tool result that preceded it has a median of **3.31**, and one 61,526-character read produced only
1,212 cache-creation tokens the following turn. Cache-breakpoint placement decouples the two.

So: the invariant is `Σ direct + Σ carry + Σ output + unattributed ≡ session total`. Turn-level
joins are best-effort inputs to that reconciliation, not assertions. Whatever doesn't join lands
in **unattributed**, visibly (FR-012, FR-013).

### 5.4 Model invalidation events as first-class causes

When a turn shows `cache_creation` far above what newly-resident content explains, **and** a
prefix-tier change is detectable between turns (tool set changed, model changed, instruction
content changed), the excess is attributed to a **cache-invalidation event** — its own entity with
its own cost — rather than smeared across files. This is what makes "what did adding that MCP
server cost me?" answerable, and it is the same machinery that prevents the misattribution in §3.

### 5.5 Downgrade confidence rather than guessing

Some inputs aren't always observable — the TTL in effect for a given request is the clearest case.
Where a needed input is missing, the figure carries a lower confidence and states its basis, or is
withheld entirely (FR-014, FR-019). *Missing attribution beats wrong attribution.* A tool that
silently assumed a 5-minute TTL would understate re-write cost by up to 60% and never say so.

---

## 6. What this means for the manager question

The disputed claim is that `.md` files dominate spend. The mechanics above split it into two
questions with different answers, which is why both must be shown side by side:

- **Always-resident instruction content** (CLAUDE.md, skills, tool/MCP schemas) is ~52–58k tokens,
  of which CLAUDE.md + skills is only ~3–4.6k — tool and MCP schemas are roughly **50×** the
  instruction files. Most of it sits in the 0.1× lane *if it caches*, which §2 says is not
  guaranteed.
- **Work-driven file reads** are where docs actually dominate: ~53% of direct tokens and ~60%
  residency-weighted, with `.md` the most-read extension by call count.

So the manager is right about file reads and wrong about instruction files — and the biggest
lever neither party proposed is pruning tool/MCP schemas, which are both the largest resident
block and the one whose change forces the most expensive re-writes (§3).

---

## Sources

Rates, thresholds, and the invalidation tiers are from Anthropic's prompt-caching documentation as
loaded 2026-08-11. Token measurements are from the 23-session local corpus documented in
`docs/research/prior-art.md` and `prior-art-pass-2.md` (local-only). Re-verify the threshold table
when adding support for a new model — it has not moved monotonically before and may not again.
