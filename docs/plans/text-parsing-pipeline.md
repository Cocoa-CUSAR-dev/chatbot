# Free-text → typed-value parsing pipeline

Implements [`text-parsing-pipeline copy.mmd`](<./text-parsing-pipeline%20copy.mmd>)
— the fixed-parser-only flow actually built here (no LLM fallback in this
phase). [`text-parsing-pipeline.mmd`](./text-parsing-pipeline.mmd) is the
earlier, superseded draft that still has an LLM-fallback branch; kept
alongside as a reasonable starting point for that future phase, not
something this implementation follows.

## Problem

A farmer answering a guided-flow question sometimes types a value in a
form the system doesn't understand as-is — a spelled-out Thai number
("เก้าร้อย") instead of digits, a relative date ("วันนี้") instead of an
ISO date, or a choice label with a trailing politeness particle ("ใช่ครับ")
instead of the exact button label. Before this, all of these failed
`validate_answer` / the choice-match step outright and re-asked the
question with no indication of *why* the answer wasn't understood.

## What this is (and isn't)

A **normalization step slotted in before validation**, not a replacement
for it. Every answer — whether it arrived already well-formed or was
normalized by this module — still passes through the exact same
`validate_answer` (INT/FLOAT/DATE/DATETIME) or choice-match (OPTION/BOOLEAN)
logic that existed before. This module never stores a value validation
didn't approve, and never invents an answer the farmer didn't give.

It is **not** an LLM step. Per the diagram, the LLM-fallback branch from the
earlier design discussion was cut from this phase — when the fixed parser
can't make sense of an answer, the farmer's original text falls through to
the existing validator/matcher, which rejects it the same way it always
has. That's a deliberate simplification, not an oversight: the LLM step
remains a reasonable future phase, on a separate, currently-unwritten
diagram.

## Scope

| `validation_rule["type"]` / question shape | Handled by | New? |
|---|---|---|
| `INT`, `FLOAT` | `text_parsing.parse_number` | ✅ |
| `DATE`, `DATETIME` | `text_parsing.parse_date` | ✅ |
| `OPTION`, `BOOLEAN` (real form questions) | `text_parsing.match_choice` | ✅ |
| `VARCHAR` | unchanged — no `type` this module recognizes, falls straight through | — |
| `GEODATA` | unchanged, same reason | — |
| Parent-picker choices (`_handle_parent_answer`, `parent_picker.choices_for`) | **left untouched, deliberately** | — |

The parent-picker step (picking which farm activity/harvest/batch a
submission belongs to) was scoped out on purpose: its choices are real
domain records with data-driven labels (dates, place names), not the small,
fixed label sets (`ใช่`/`ไม่`, a handful of form choices) this module's
particle-stripping is safe against. Fuzzy-matching those risks resolving to
the wrong record — a materially worse failure than one extra re-ask.

## Design

### Two separate integration points, not one

The original diagram drew a single "Fixed parser" box. Reading
`src/conversation/service.py` found this is actually two independent
mechanisms in `handle_answer`, wired separately:

1. **Before `validate_answer`** (`INT`/`FLOAT`/`DATE`/`DATETIME`) —
   `text_parsing.try_fixed_parse(rule_type, raw_text)` runs first. If it
   returns a value, that normalized value — not the original text — is
   what `validate_answer` checks and what eventually gets stored. If it
   returns `None` (couldn't parse), `raw_text` is passed to
   `validate_answer` **unchanged**, exactly as before this module existed.
2. **The choice-match step** (`OPTION`/`BOOLEAN`, real form questions,
   `page_choices` loop) — `text_parsing.match_choice(raw_text, page_choices)`
   replaces the old bare `c.label == raw_text` exact check. Exact match is
   still tried first and is byte-for-byte the same as before; only when
   that fails does particle-stripping kick in.

### Why a parse failure falls through to the field's own error message

The first design attempt returned a **generic** "couldn't understand"
message on a parse failure, bypassing `validate_answer` entirely. That
regressed a real, tested behavior: a field author writes one
`error_message` in `form.field_validation_rule` (e.g. *"กรุณากรอกจำนวนพัดลม
เป็นจำนวนเต็ม 0-50"*), and every kind of invalid answer to that field — out
of range, non-numeric, anything — is supposed to show that same message.
Splitting "couldn't parse" into a separate, generic message broke that
(caught by `tests/integration/test_question_validation.py`, which was
never touched by this change and still expects the field's own message).

The fix: a parse failure is treated as if this module doesn't exist for
that answer — `raw_text` flows to `validate_answer` unchanged, which
rejects it on its own terms with the field's own configured message. One
message per field, regardless of *why* it was rejected.

### The Buddhist-Era year trap

`dateparser` has no Thai-calendar awareness: it parses a Thai Buddhist Era
year like `2569` as a literal (and 543 years wrong) Gregorian year. The
correction has to be narrow: only a year within a few years of *this
Gregorian year + 543* is treated as BE and shifted back. The first
attempt instead flagged "any year further out than a few years from now,"
which also caught an ordinary, deliberately-far-future Gregorian test date
(`2099`, used elsewhere to test "reject a future date") and silently
mangled it to `1556` — caught by `tests/integration/test_question_validation.py`'s
existing DATE/DATETIME cases.

### Why particle-stripping instead of fuzzy/similarity matching

The first attempt used `difflib.get_close_matches` for `match_choice`.
Caught by this module's own test suite: `"farm 15"` scores **0.92**
similarity against `"farm 1"` — a plausible-looking cutoff would still
accept it, silently resolving to the wrong choice (this repo's own OPTION
fields, e.g. `farm_id`, are exactly this kind of short/near-identical
label). Fuzzy string similarity is the wrong tool for an enumerated,
machine-generated label set. Stripping a small, fixed list of trailing Thai
politeness particles (ครับ/ค่ะ/นะ/...) before re-trying an *exact* match
can't cause that kind of collision — it only ever shortens text by a known
suffix, never turns one distinct label into another.

## Libraries used (and why)

Per team decision, hand-rolling Thai number-word or date grammar was
rejected in favor of existing libraries — narrower, better-tested, and
someone else's job to keep correct for edge cases neither this module nor
its tests would think to cover.

- **[`pythainlp`](https://pythainlp.org/)** — `pythainlp.util.thaiword_to_num`
  converts spelled-out Thai numerals (`"เก้าร้อยห้าสิบเอ็ด"` → `951`) to an
  `int`, raising `ValueError` on anything it can't parse. Verified directly
  against the installed version (5.3.7) before writing any code —
  including that it does **not** tolerate internal spaces or a trailing
  unit word (`"เก้าร้อยกิโล"` raises), which is why `parse_number` doesn't
  try to strip units itself; per team decision, that's out of scope, not a
  bug to work around.
- **[`dateparser`](https://dateparser.readthedocs.io/)** — `dateparser.parse(text,
  languages=["th"])` resolves both relative Thai phrases (`วันนี้`/
  `พรุ่งนี้`/`เมื่อวาน`/`เมื่อวานซืน`) and absolute dates in mixed formats,
  returning `None` (never raising) when it can't. Verified directly
  (version 1.4.3) that it has no Buddhist Era awareness at all — hence the
  explicit correction step above. `"มะรืนนี้"` (day after tomorrow) is
  **not** resolved by dateparser either (returns `None`) — left as-is,
  since per team decision an unresolved date should fall through to a
  re-ask, not grow a hand-rolled keyword list to cover it.
- Thai digit characters (`๐`–`๙`) needed **no library or translation
  table at all** — Python's built-in `int()`/`float()` already parse them
  identically to ASCII digits.

## Files changed

| File | Change |
|---|---|
| `src/conversation/text_parsing.py` | New. `parse_number`, `parse_date`, `match_choice`, `try_fixed_parse`, `FIXED_PARSE_TYPES`. |
| `src/conversation/service.py` | `handle_answer`: fixed-parse step before `validate_answer`; choice-match call site now goes through `text_parsing.match_choice`. |
| `pyproject.toml` | Added `pythainlp>=5.0`, `dateparser>=1.2`. |
| `tests/conversation/test_text_parsing.py` | New. Unit tests for all four functions, including the two regressions found while writing them (see Design above). |
| `tests/conversation/test_service.py` | New `handle_answer`-level cases: spelled-out number normalized before storing, unparseable number still shows the field's own error, relative Thai date normalized, choice with a trailing particle resolves. |

## Verified

```
ruff check / ruff format --check   -- clean
mypy src                           -- clean
pytest (unit, no DB)               -- 266 passed, 31 skipped
pytest (RUN_DB_TESTS=1, real Postgres 18, full schema.sql -- same as CI)
                                    -- 297 passed, 0 failed
```

The full-suite, real-Postgres run is what actually caught both design bugs
described above — the no-DB unit run alone passed throughout and would not
have caught either one, since the regressions were in
`tests/integration/test_question_validation.py` (DB-backed, pre-existing,
untouched by this change).

## Explicitly out of scope (this phase)

- **LLM fallback** for text the fixed parser can't handle — cut from the
  diagram this implements; a reasonable future phase, not designed further
  here.
- **Parent-picker choice matching** (`_handle_parent_answer`) — scoped out
  above; real domain-record labels are the wrong shape for even
  particle-stripping to be obviously safe against without real data to
  check it against.
- **Unit-stripping for numbers** (`"900 กิโล"`) or **extracting a number
  from a longer sentence** — per team decision, text parsing is a narrow
  backup for a bare spelled-out number, not a general extractor.
- **`"มะรืนนี้"` / other relative-date phrases `dateparser` doesn't
  resolve** — left to fall through to a re-ask rather than hand-rolling a
  keyword list to cover what the library doesn't.
