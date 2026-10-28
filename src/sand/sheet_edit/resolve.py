"""Ask the LLM what an instruction means: two passes, compared.

The call itself is injected (`CallLLM`), so this module never touches a
network and the workflow decides which model answers. Each pass gets a
system prompt, the catalog table followed by the instruction, and the
sheet's own schema (`proposed_edits_schema`: column ids and sample labels
are enums, so an id that does not exist cannot be produced).

One model is available, so the two passes differ by what they see, not
by chance: pass A reads the table in sheet order with the rules as
written, pass B the steps in reverse order with the rules reworded. Both
run at temperature 0, which the workflow sets. Only edits both passes
agree on go on as edits; the rest becomes `Uncertain`, shown with both
readings and never applied without the user choosing. Same model means
shared blind spots: this is one more layer, the deterministic checks in
ground.py are the safety net.
"""

import json
import re
from collections.abc import Callable

from pydantic import ValidationError

from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.edits import (
    Edit,
    ProposedEdits,
    Range,
    Samples,
    Uncertain,
    Unreadable,
    Unresolved,
    proposed_edits_model,
    proposed_edits_schema,
)
from sand.sheet_edit.render import table

# recorded with every proposal (changelog, status); bump on any prompt change
PROMPT_VERSION = 'v1'

# (system prompt, user text, JSON schema) -> the model's answer, as text
CallLLM = Callable[[str, str, dict], str]

_TASK = """\
You turn ONE instruction about a lab spreadsheet into cell edits. The user \
is a researcher who uploaded the sheet and says, by voice or text, what to \
change. The table lists every column with its ID, step, parameter and \
current values, then the samples. Answer ONLY JSON matching the schema."""

_RULES_A = """\
Rules:
- Use only words the user said. Never add a value, a sample or a column the \
user did not mention.
- "said": the exact words of the instruction this edit comes from, copied \
verbatim, nothing else.
- "new": the value as the user said it. Never convert units, scale or round.
- "old": what the table shows the cell holds now; null when it is empty.
- "rows": "all" when the user names no samples; the samples named; a range \
("samples 1 to 3"); or "where" another column holds the value the user \
pointed through ("the time of the DMSO step").
- A value the user mentions as current ("the 3000") is a clue to the cell: \
use it as "old" or as the "where" value.
- If the words fit more than one column, do not pick one: answer \
"ambiguous" with every candidate.
- If what the user points at is not in the table, answer "not_found".
- If the user corrects themselves ("4000, no, 5000"), make ONE edit with \
the value said last.
- One edit per column and group of samples. Nothing the user asked for may \
be dropped: what you cannot place goes to "unresolved"."""

_RULES_B = """\
How to answer:
- Every edit must come from the user's words and nothing else. No value, \
sample or column may be invented.
- Copy the user's exact words for this edit into "said", unchanged.
- Put the value in "new" exactly as spoken: no unit conversion, no \
rounding, no rescaling.
- "old" is the current value shown in the table for that cell, or null for \
an empty cell.
- Choose "rows" as "all" if no sample is named, else the named samples, a \
"first"/"last" range for "samples 1 to 3", or "where" when the user names \
a value in another column to point at rows ("the DMSO one").
- When the user quotes a current value, it identifies the cell: it is the \
"old" value or the "where" value.
- Several columns could be meant? Then "ambiguous" with all of them, never \
a guess.
- Nothing in the table matches? Then "not_found".
- A self-correction ("4000, no, 5000") is ONE edit with the last value.
- One edit per column and sample group. Do not drop any part of the \
instruction: anything you cannot place is "unresolved"."""


def system_prompt(reverse: bool) -> str:
    return f'{_TASK}\n\n{_RULES_B if reverse else _RULES_A}'


def user_text(instruction: str, catalog: Catalog, reverse: bool) -> str:
    return f'{table(catalog, reverse=reverse)}\nInstruction: {instruction}'


def resolve(instruction: str, catalog: Catalog, call: CallLLM) -> ProposedEdits:
    """Two passes, agreed. One unreadable answer makes the whole
    instruction unresolved: half a reading is no reading."""
    schema = proposed_edits_schema(catalog)
    passes = [
        _pass(instruction, catalog, call, schema, reverse) for reverse in (False, True)
    ]
    unreadable = [p for p in passes if isinstance(p, Unreadable)]
    if unreadable:
        return ProposedEdits(edits=[], unresolved=unreadable[:1])
    a, b = passes
    assert isinstance(a, ProposedEdits) and isinstance(b, ProposedEdits)
    return _agreed(a, b, catalog)


def _pass(
    instruction: str, catalog: Catalog, call: CallLLM, schema: dict, reverse: bool
) -> ProposedEdits | Unreadable:
    answer = call(
        system_prompt(reverse), user_text(instruction, catalog, reverse), schema
    )
    try:
        data = json.loads(answer)
        narrowed = proposed_edits_model(catalog).model_validate(data)
    except (ValueError, ValidationError) as why:  # JSONDecodeError is a ValueError
        detail = str(why).splitlines()[0]
        return Unreadable(reason='unreadable', detail=detail, said=instruction)
    # back to the plain models: the narrowed ones only exist to validate
    return ProposedEdits.model_validate(narrowed.model_dump())


def _agreed(a: ProposedEdits, b: ProposedEdits, catalog: Catalog) -> ProposedEdits:
    """Edits in both passes stay, in pass A's order. An edit in one pass
    only is uncertain: paired with the other pass's edit for the same
    words when there is one. Unresolved items of either pass are kept."""
    keys_a = {_edit_key(e, catalog): e for e in a.edits}
    keys_b = {_edit_key(e, catalog): e for e in b.edits}
    edits = [e for k, e in keys_a.items() if k in keys_b]
    only_a = [e for k, e in keys_a.items() if k not in keys_b]
    only_b = [e for k, e in keys_b.items() if k not in keys_a]
    uncertain: list[Unresolved] = []
    for edit in only_a:
        other = next((e for e in only_b if _words(e.said) == _words(edit.said)), None)
        readings = [edit] if other is None else [edit, other]
        if other is not None:
            only_b.remove(other)
        uncertain.append(
            Uncertain(reason='uncertain', readings=readings, said=edit.said)
        )
    uncertain += [
        Uncertain(reason='uncertain', readings=[e], said=e.said) for e in only_b
    ]
    seen: set[tuple] = set()
    unresolved: list[Unresolved] = []
    for item in [*a.unresolved, *b.unresolved]:
        key = (item.reason, _words(item.said), str(getattr(item, 'candidates', '')))
        if key not in seen:
            seen.add(key)
            unresolved.append(item)
    return ProposedEdits(edits=edits, unresolved=[*uncertain, *unresolved])


def _edit_key(edit: Edit, catalog: Catalog) -> tuple:
    """What two passes must agree on. `old` is left out: ground checks it
    against the sheet anyway, and the passes may describe the same cell
    with and without it."""
    anchor = (edit.anchor.column, _words(edit.anchor.equals)) if edit.anchor else None
    return (
        edit.target,
        _rows_key(edit.rows, catalog),
        anchor,
        _words(edit.new),
        _words(edit.said),
    )


def _rows_key(rows, catalog: Catalog):
    """A range and the list of the same samples are the same rows."""
    if rows == 'all':
        return 'all'
    if isinstance(rows, Samples):
        return (frozenset(rows.samples), rows.subbatch)
    if isinstance(rows, Range):
        labels = [s.label for s in catalog.samples if s.label is not None]
        labels = list(dict.fromkeys(labels))
        if rows.first in labels and rows.last in labels:
            first, last = labels.index(rows.first), labels.index(rows.last)
            if first <= last:
                return (frozenset(labels[first : last + 1]), rows.subbatch)
        return ('range', rows.first, rows.last, rows.subbatch)
    return ('where', rows.where, _words(rows.equals))


def _words(text: str | None) -> str | None:
    """Spacing and case are no disagreement."""
    return None if text is None else re.sub(r'\s+', ' ', text).strip().casefold()
