# Golden: `session_basic` — hand-verified derivation

**A diff in the accompanying test is a red alert, never a rebaseline.** It means either a real
regression or a deliberate change to the cost model, and the latter requires written
justification and human sign-off (constitution Principle V).

Every figure below was computed **by hand from the rates**, then compared against the tool's
output — not read out of the tool and blessed. If you change the model, redo the arithmetic
here first and only then touch `expected.json`.

## Model change — reviewed and accepted, 2026-08-11

**What changed.** Carry cost is now split across the items in the **cached lane** on a turn,
rather than across every item that happened to be resident.

**Why the old behaviour was wrong.** On turn 2 of this fixture, `b.md` is being *written* into
the cache — it is charged the 1.25× write rate. The old model also gave it a share of that same
turn's 0.1× read charge, which billed one piece of content twice, at two different rates, in a
single turn. `a.py`, which really was being re-shown from cache, was correspondingly
under-charged.

**Effect here.** Turn 2's 500 micro-dollar read charge now goes entirely to `a.py` (previously
167 to `a.py`, 333 to `b.md`). Session totals are unchanged — this moves cost between items, it
does not create or destroy any.

**Fixture change in the same review.** A fifth turn was added (turn 3) in which both files are
cached and neither is being written. It exists to keep an *uneven* proportional split in the
golden: 500 across weights 1000 and 2000 floors to 166 + 333 = 499, and the largest-remainder
rule has to place the missing micro-dollar. Without it the change above would have removed the
only case in this fixture where that rule is exercised.

## Rates in effect

Model `claude-opus-5` on every turn, from `src/claude_cost_tracker/config/pricing.toml`:

| Component | Rate | Micro-dollars per token |
|---|---|---:|
| input | $5.00 / MTok × 1 | 5 |
| cache write, 5-minute window | $5.00 / MTok × 1.25 | 6.25 |
| cache read | $5.00 / MTok × 0.1 | 0.5 |
| output | $25.00 / MTok | 25 |

## Item sizes

Both files are sized by the declared (character-based) tier at 4 characters per token, so the
fixture uses exact multiples of 4 to keep the arithmetic checkable:

| Item | Characters | Tokens |
|---|---:|---:|
| `/repo/src/a.py` | 4,000 | 1,000 |
| `/repo/docs/b.md` | 8,000 | 2,000 |

## Per-turn charges

| Turn | Observed usage | Arithmetic | Total |
|---|---|---|---:|
| 0 | 1,000 input, 200 output | 1,000×5 = 5,000; 200×25 = 5,000 | **10,000** |
| 1 | 1,000 cache write (5m), 100 output | 1,000×6.25 = 6,250; 100×25 = 2,500 | **8,750** |
| 2 | 2,000 cache write (5m), 1,000 cache read, 100 output | 2,000×6.25 = 12,500; 1,000×0.5 = 500; 100×25 = 2,500 | **15,500** |
| 3 | 1,000 cache read, 100 output | 1,000×0.5 = 500; 100×25 = 2,500 | **3,000** |
| 4 | 2,000 cache read, 40 output | 2,000×0.5 = 1,000; 40×25 = 1,000 | **2,000** |

**Session total = 10,000 + 8,750 + 15,500 + 3,000 + 2,000 = 39,250 micro-dollars ($0.03925).**

## Attribution

**Turn 0.** Nothing is resident yet. The 5,000 of fresh input is conversation overhead and the
5,000 of output belongs to the exchange — never to a file (invariant A2).

**Turn 1.** `a.py` arrived after turn 0, so it is first paid for here. It is the only injection,
so the whole 6,250 write is its direct cost.

**Turn 2.** `b.md` arrived after turn 1 and takes the whole 12,500 write. The 500 read charge
goes entirely to **`a.py`**: it is the only item in the cached lane, because `b.md` is being
written this turn and is already paying the write rate for it.

**Turn 3.** Both files are cached and neither is being written, so the 500 read charge divides
between them by token weight:

- `a.py`: 500 × 1,000/3,000 = 166.67 → floor 166, remainder 2,000/3,000
- `b.md`: 500 × 2,000/3,000 = 333.33 → floor 333, remainder 1,000/3,000
- Floors sum to 499, one micro-dollar short. Largest-remainder gives it to `a.py`.
- **`a.py` = 167, `b.md` = 333.** They sum to 500 exactly (invariant A3).

**Turn 4.** The compaction boundary before it names no survivors, so both files were evicted and
carry stops (FR-004). The 1,000 read charge therefore has **no resident item to explain it** and
becomes the unattributed remainder — it is not spread across the files that used to be there
(FR-013). The 1,000 of output still belongs to the exchange.

## Reconciliation

| | Micro-dollars |
|---|---:|
| overhead | 5,000 |
| output | 5,000 + 2,500 + 2,500 + 2,500 + 1,000 = 13,500 |
| direct | 6,250 + 12,500 = 18,750 |
| carry | 500 + 167 + 333 = 1,000 |
| **attributed** | 5,000 + 13,500 + 18,750 + 1,000 = **38,250** |
| **unattributed** | **1,000** |
| **total** | **39,250** |

38,250 + 1,000 = 39,250. Exact integer equality, zero tolerance (invariant A1, SC-001).

Per item: `a.py` carries 500 + 167 = **667**; `b.md` carries **333**.

Unattributed share = 1,000 / 39,250 = **2.55%**, comfortably inside the 15% ceiling of SC-002 —
and displayed regardless.

## What this fixture deliberately exercises

- All four components, each at its own rate, including the 1.25× write multiplier.
- **Lane separation**: content being written is not also charged the read rate (turn 2).
- A largest-remainder split that does **not** divide evenly (turn 3), which is where a naive
  proportional split silently loses a micro-dollar.
- Compaction eviction ending carry, read from the boundary's own survivor list.
- A **non-zero unattributed remainder** with a real cause — cost that arrived after everything
  it could have been attributed to was evicted.
