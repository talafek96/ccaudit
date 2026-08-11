# Golden: `session_basic` — hand-verified derivation

**A diff in the accompanying test is a red alert, never a rebaseline.** It means either a real
regression or a deliberate change to the cost model, and the latter requires written
justification and human sign-off (constitution Principle V).

Every figure below was computed **by hand from the rates**, then compared against the tool's
output — not read out of the tool and blessed. If you change the model, redo the arithmetic
here first and only then touch `expected.json`.

## Rates in effect

Model `claude-opus-5` on every turn, from `src/ccaudit/config/pricing.toml`:

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
| 3 | 2,000 cache read, 40 output | 2,000×0.5 = 1,000; 40×25 = 1,000 | **2,000** |

**Session total = 10,000 + 8,750 + 15,500 + 2,000 = 36,250 micro-dollars ($0.03625).**

## Attribution

**Turn 0.** Nothing is resident yet. The 5,000 of fresh input is conversation overhead and the
5,000 of output belongs to the exchange — never to a file (invariant A2).

**Turn 1.** `a.py` arrived after turn 0, so it is first paid for here. It is the only injection,
so the whole 6,250 write is its direct cost.

**Turn 2.** `b.md` arrived after turn 1 and takes the whole 12,500 write. The 500 read charge is
shared between both resident files, split by token weight:

- `a.py`: 500 × 1,000/3,000 = 166.67 → floor 166, remainder 2,000/3,000
- `b.md`: 500 × 2,000/3,000 = 333.33 → floor 333, remainder 1,000/3,000
- Floors sum to 499, one micro-dollar short. Largest-remainder gives it to `a.py`.
- **`a.py` = 167, `b.md` = 333.** They sum to 500 exactly (invariant A3).

**Turn 3.** The compaction boundary before it names no survivors, so both files were evicted and
carry stops (FR-004). The 1,000 read charge therefore has **no resident item to explain it** and
becomes the unattributed remainder — it is not spread across the files that used to be there
(FR-013). The 1,000 of output still belongs to the exchange.

## Reconciliation

| | Micro-dollars |
|---|---:|
| overhead | 5,000 |
| output | 5,000 + 2,500 + 2,500 + 1,000 = 11,000 |
| direct | 6,250 + 12,500 = 18,750 |
| carry | 167 + 333 = 500 |
| **attributed** | 5,000 + 11,000 + 18,750 + 500 = **35,250** |
| **unattributed** | **1,000** |
| **total** | **36,250** |

35,250 + 1,000 = 36,250. Exact integer equality, zero tolerance (invariant A1, SC-001).

Unattributed share = 1,000 / 36,250 = **2.76%**, comfortably inside the 15% ceiling of SC-002 —
and displayed regardless.

## What this fixture deliberately exercises

- All four components, each at its own rate, including the 1.25× write multiplier.
- A largest-remainder split that does **not** divide evenly, which is where a naive
  proportional split silently loses a micro-dollar.
- Compaction eviction ending carry, read from the boundary's own survivor list.
- A **non-zero unattributed remainder** with a real cause — cost that arrived after everything
  it could have been attributed to was evicted.
