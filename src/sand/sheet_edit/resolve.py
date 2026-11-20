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
agree on go on as edits; the rest becomes `Disputed`, shown with every
reading and never applied without the user choosing. Same model means
shared blind spots: this is one more layer, the deterministic checks in
ground.py are the safety net.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.coverage import quote_words
from sand.sheet_edit.edits import (
    Ambiguous,
    Disputed,
    Edit,
    ProposedEdits,
    Range,
    RowSpec,
    Samples,
    Unreadable,
    Unresolved,
    proposed_edits_model,
    proposed_edits_schema,
)
from sand.sheet_edit.ground import Refusal, resolve_rows, typed_value
from sand.sheet_edit.numbers import value
from sand.sheet_edit.render import table
from sand.sheet_edit.workbook import SAMPLE_HEADER, SUBBATCH_HEADER, info_column

# recorded with every proposal (changelog, status); bump on any change to
# the prompts below or to the table in render.py, which the model reads
PROMPT_VERSION = 'v7'
# the job's cache keeps proposals after `agree`: bump the second part on
# a change to how two opinions agree, so agreed answers are asked again
CACHE_VERSION = f'{PROMPT_VERSION}+agree-6'

# (system prompt, user text, JSON schema) -> the model's answer, as text.
# The schema is for the provider's structured output; the answer is still
# validated here (see Unreadable), since a provider may enforce only part
# of it. An answer that is not text, or not the schema, makes the
# instruction unreadable. Transport errors are the caller's to raise.
CallLLM = Callable[[str, str, dict], str]

_TASK = """\
You turn ONE instruction about a lab spreadsheet into cell edits. The user \
is a researcher who uploaded the sheet and says, by voice or text, what to \
change. The table lists every column with its ID, step, parameter and \
current values (in quotes; an empty cell shows as (empty)), then the \
samples. Answer ONLY JSON matching the schema."""

_RULES_A = """\
Rules:
- Use only words the user said. Never add a value, a sample or a column the \
user did not mention.
- "said": the exact words of the instruction this edit comes from, copied \
verbatim, nothing else.
- "new": the value as the user said it, without its unit. Never convert \
units, scale or round. To clear a cell, "new" is "" (the user said to \
clear, empty or remove it); (empty) is never a value.
- "unit": the unit the user said with the number, as said ("2 hours": \
"new" "2", "unit" "hours"; "500 rpm/s": "500" and "rpm/s"), even when it \
differs from the column's unit; null when the user said no unit. Text \
values have no unit.
- "old": the current value the table shows for the cell, without the quotes; \
null when the cell is empty or when the samples hold different values.
- "said_old": the current value as the user said it, even when the table \
shows another ("from 2000 to 4000": "2000"; "Cu instead of Ag": "Ag"); \
null when the user names no current value.
- "rows": "all" when the user names no samples; the samples named; a range \
("samples 1 to 3"); or "where" another column holds a value, to pick the \
rows by it ("the samples with variation 20min": where = that column, \
equals = "20min").
- "anchor": when the user names a value in a neighbouring column of the \
same step to say which cell they mean ("the volume of the DMSO": the \
volume column is the target, anchor = the solvent column, equals = "DMSO"). \
"equals" is the value the user named, spelled as the table spells it \
(their "ethanol" is the table's "Ethanol"), never another value of that \
column: if no row holds it, answer "not_found".
- A value the user mentions as current ("the 3000") is a clue to the cell \
and goes into "said_old"; "old" stays what the table shows.
- If the words fit more than one column, do not pick one: answer \
"ambiguous" with every candidate.
- The same parameter is often in several steps (a rotation speed in every \
spin coating). A step number, or a material or value only one of those \
steps holds, picks the step; the step type alone ("spin coating" when \
several steps are spin coatings) does not. If nothing picks one, answer \
"ambiguous" with every step's column. Never put the change into several \
steps to cover them, unless the user asked for every step ("in all spin \
coatings"). When only one step has the parameter, no step needs naming.
- If what the user points at is not in the table, answer "not_found".
- One edit per column and group of samples. Nothing the user asked for may \
be dropped: what you cannot place goes to "unresolved"."""

_RULES_B = """\
How to answer:
- Every edit must come from the user's words and nothing else. Never invent \
a value, a sample or a column the user did not say.
- Copy the user's exact words for this edit into "said", unchanged.
- Put the value in "new" exactly as spoken, without its unit: no unit \
conversion, no rounding, no rescaling. The unit the user said with it goes \
in "unit" as spoken ("2 hours": "2" and "hours"), whatever unit the column \
has; "unit" is null if the user said none, and for text values. Clearing a cell \
(the user says clear, empty, remove) is "new": ""; the marker (empty) is \
not a value.
- "old" is the current value shown in the table for that cell, quotes \
left out, or null for an empty cell or samples that hold different values.
- "said_old" is the value the user says the cell holds now, in their words \
("4000 instead of 2000": "2000"), whatever the table shows; null if they \
say none.
- Choose "rows" as "all" if no sample is named, else the named samples, a \
"first"/"last" range for "samples 1 to 3", or "where" when the user picks \
rows by what another column holds ("the samples with variation 20min").
- Use "anchor" when the user identifies the cell by a value next to it in \
the same step ("the volume of the DMSO" edits the volume column; the \
anchor is the solvent column with equals "DMSO"). The anchor's "equals" \
is the value the user named, in the table's spelling ("ethanol" said, \
"Ethanol" shown: "Ethanol"), never a different value the table shows \
instead: when no row of that column holds it, it is "not_found".
- When the user quotes a current value ("the 3000"), it identifies the \
cell and is the "said_old"; "old" is still the table's value.
- Several columns could be meant? Then "ambiguous" with all of them, never \
a guess.
- A parameter that several steps have (each spin coating has a rotation \
speed) needs the user to say which step: a step number, or a material or \
type only one of those steps has. Without it the answer is "ambiguous", \
listing that column of every such step; spreading the change over several \
steps is not an answer, except when the user says every step ("all spin \
coatings"). A parameter only one step has needs no step.
- Nothing in the table matches? Then "not_found".
- One edit per column and sample group. Do not drop any part of the \
instruction: anything you cannot place is "unresolved"."""


def system_prompt(second_opinion: bool) -> str:
    """The first pass gets the rules as written, the second opinion the
    same rules in other words."""
    return f'{_TASK}\n\n{_RULES_B if second_opinion else _RULES_A}'


def user_text(instruction: str, catalog: Catalog, second_opinion: bool) -> str:
    """The first pass reads the table in sheet order, the second opinion
    the steps last to first."""
    return f'{table(catalog, reverse=second_opinion)}\nInstruction: {instruction}'


@dataclass(frozen=True)
class Prompt:
    """One pass's question to the model. `schema` is the sheet's own
    (`proposed_edits_schema`), the same for both passes."""

    system: str
    text: str
    schema: dict
    second_opinion: bool


Opinion = ProposedEdits | Unreadable


def prompts(instruction: str, catalog: Catalog) -> list[Prompt]:
    """The two passes' prompts, the first opinion first."""
    schema = proposed_edits_schema(catalog)
    return [
        Prompt(
            system=system_prompt(second_opinion),
            text=user_text(instruction, catalog, second_opinion),
            schema=schema,
            second_opinion=second_opinion,
        )
        for second_opinion in (False, True)
    ]


def resolve(instruction: str, catalog: Catalog, call: CallLLM) -> ProposedEdits:
    """Two passes, agreed, with `call` answering both. The workflow does
    the same in three steps: `prompts`, its own calls, `read_answer` and
    `agree`."""
    opinions = [
        read_answer(call(p.system, p.text, p.schema), instruction, catalog)
        for p in prompts(instruction, catalog)
    ]
    return agree(opinions, catalog)


def read_answer(answer: object, instruction: str, catalog: Catalog) -> Opinion:
    """One pass's answer, validated against the sheet's schema."""
    if not isinstance(answer, str):
        return Unreadable(
            reason='unreadable', detail='the answer is not text', said=instruction
        )
    model = proposed_edits_model(catalog)
    try:
        narrowed = model.model_validate(json.loads(answer))
    except ValueError as why:  # JSON errors and pydantic's ValidationError both
        detail = str(why).partition('\n')[0] or type(why).__name__
        return Unreadable(reason='unreadable', detail=detail, said=instruction)
    # back to the plain models: the narrowed ones only exist to validate
    return ProposedEdits.model_validate(narrowed.model_dump())


def agree(opinions: Sequence[Opinion], catalog: Catalog) -> ProposedEdits:
    """The two opinions, in the order of `prompts`, compared. One
    unreadable answer makes the whole instruction unresolved: half a
    reading is no reading."""
    if len(opinions) != 2:  # noqa: PLR2004
        raise ValueError(f'two opinions are compared, not {len(opinions)}')
    a, b = opinions
    for opinion in (a, b):
        if isinstance(opinion, Unreadable):
            return ProposedEdits(edits=[], unresolved=[opinion])
    assert isinstance(a, ProposedEdits) and isinstance(b, ProposedEdits)
    return _agreed(a, b, catalog)


def _agreed(a: ProposedEdits, b: ProposedEdits, catalog: Catalog) -> ProposedEdits:
    """Edits in both passes stay, in pass A's order, unless a pass also
    left the same words unresolved. Every other edit is disputed (see
    _disputes)."""
    by_a, by_b = _by_write(a.edits, catalog), _by_write(b.edits, catalog)
    hedges = {_quote(u.said) for u in [*a.unresolved, *b.unresolved]}
    edits: list[Edit] = []
    # (pass, edit, the quotes of its write's repeats): not agreed on
    doubts: list[tuple[int, Edit, set[str]]] = []

    for key, group in by_a.items():
        edit = group[0]
        if key not in by_b:
            doubts += [(0, r, _quotes(group)) for r in _readings(group)]
            continue
        both = [*group, *by_b[key]]
        pointed = _first_per_anchor(both)
        olds = _first_per_said_old(both)
        # the same write pointed at through different cells, or with two
        # old values heard: each is a reading
        differ = next((d for d in (pointed, olds) if len(d) > 1), {})
        # the passes (and repeats) may cut the quote differently: a hedge
        # on words around any of these quotes counts
        hedged = any(_overlap(h, q) for h in hedges for q in _quotes(both))
        if differ or hedged:
            doubts += [
                (side, r, _quotes(both))
                for side, own in enumerate((group, by_b[key]))
                for r in _readings(own)
            ]
        else:
            # after _kept, which may take another pass's edit for its anchor
            edits.append(_named(_kept(edit, pointed, olds), both))
    for key, group in by_b.items():
        if key not in by_a:
            doubts += [(1, r, _quotes(group)) for r in _readings(group)]
    unresolved = _disputes(doubts, a.unresolved, b.unresolved)
    return ProposedEdits(edits=edits, unresolved=unresolved)


def _disputes(
    doubts: list[tuple[int, Edit, set[str]]],
    a: list[Unresolved],
    b: list[Unresolved],
) -> list[Unresolved]:
    """The edits not agreed on and the passes' unresolved items, grouped
    by overlapping words: a group with an edit is one Disputed, what each
    pass made of the words side by side; the other items are kept once."""
    pieces = [
        *doubts,
        *(
            (side, u, {_quote(u.said)})
            for side, items in enumerate((a, b))
            for u in items
        ),
    ]
    groups: list[list[tuple[int, Edit | Unresolved, set[str]]]] = []
    for piece in pieces:
        hit = [
            i
            for i, group in enumerate(groups)
            if any(_overlap(q, o) for q in piece[2] for _, _, qs in group for o in qs)
        ]
        if not hit:
            groups.append([piece])
            continue
        groups[hit[0]] = [*(p for i in hit for p in groups[i]), piece]
        for i in reversed(hit[1:]):
            del groups[i]
    disputed: list[Unresolved] = []
    kept: list[Unresolved] = []
    for group in groups:
        if any(isinstance(p, Edit) for _, p, _ in group):
            first, second = ([p for s, p, _ in group if s == side] for side in (0, 1))
            disputed.append(
                Disputed(
                    reason='disputed',
                    first=_once(first),
                    second=_once(second),
                    said=max((p.said for _, p, _ in group), key=len),
                )
            )
        else:
            kept += _once([p for _, p, _ in group])
    return [*disputed, *kept]


def _quotes(edits: list[Edit]) -> set[str]:
    return {_quote(e.said) for e in edits}


def _overlap(one: str, other: str) -> bool:
    """One quote within the other, by whole words."""
    return bool(one and other) and (
        f' {one} ' in f' {other} ' or f' {other} ' in f' {one} '
    )


def _once(items: list) -> list:
    """Items on overlapping words (a group of _disputes): the same item
    from both passes, or twice, is one; every "ambiguous" is one, on the
    longest quote, with the candidates of each."""
    kept: dict = {}
    for item in items:
        if isinstance(item, Ambiguous) and 'ambiguous' in kept:
            known = kept['ambiguous']
            more = [c for c in item.candidates if c not in known.candidates]
            kept['ambiguous'] = known.model_copy(
                update={
                    'candidates': [*known.candidates, *more],
                    'said': max(known.said, item.said, key=len),
                }
            )
            continue
        # edits are deduplicated by _readings already
        if isinstance(item, Edit):
            key = id(item)
        elif isinstance(item, Ambiguous):
            key = 'ambiguous'
        else:
            key = (item.reason, _quote(item.said))
        kept.setdefault(key, item)
    return list(kept.values())


# the rows of an edit, as compared between the two opinions: the sheet
# rows when the sample words resolve, else the spec as given
RowsAgreementKey = frozenset[int] | tuple
# (target, rows, new): what two opinions must agree on
AgreementKey = tuple[str, RowsAgreementKey, tuple]


def _agreement_key(edit: Edit, catalog: Catalog) -> AgreementKey:
    """What two passes must agree on: what would be written, the cells and
    the value. Left out: `old` and `said_old`, which ground checks
    against the sheet (a `said_old` of either pass is kept, two different
    ones disagree);
    `said`, which the passes may cut differently for one and the same
    edit; the anchor, which only points: an anchor of either pass is
    kept for ground to check, two different ones disagree. The value is
    compared as it would be written ('200 second' into a [s] column is
    200, like '200'); text as written: material names are case-sensitive
    (PbI2, Me-4PACz)."""
    return (
        edit.target,
        _rows_agreement_key(edit.rows, catalog),
        _value_agreement_key(edit, catalog),
    )


def _value_agreement_key(edit: Edit, catalog: Catalog) -> tuple:
    new = edit.new.strip()
    column = catalog.column(edit.target)
    if column is None:
        return ('said', new, (edit.unit or '').strip())
    written, problems = typed_value(edit, column)
    if problems:  # ground refuses it: the words are all there is
        return ('said', new, (edit.unit or '').strip())
    return ('written', type(written).__name__, written)


def _by_write(edits: list[Edit], catalog: Catalog) -> dict[AgreementKey, list[Edit]]:
    """A pass's edits by what they write: a repeat (the same write) may
    differ in `old`, `said` or the anchor."""
    groups: dict[AgreementKey, list[Edit]] = {}
    for edit in edits:
        groups.setdefault(_agreement_key(edit, catalog), []).append(edit)
    return groups


def _readings(group: list[Edit]) -> list[Edit]:
    """One pass's repeats of a write, as the user sees them: one reading
    per anchor and per old value said, or the first edit when none has
    either."""
    found = [*_first_per_anchor(group).values(), *_first_per_said_old(group).values()]
    return list({id(e): e for e in found}.values()) or group[:1]


def _kept(edit: Edit, pointed: dict, olds: dict) -> Edit:
    """A pointer or an old value either pass gave is kept, so that ground
    checks it."""
    kept = next(iter(pointed.values()), edit)
    if olds and not (kept.said_old or '').strip():
        kept = kept.model_copy(update={'said_old': next(iter(olds.values())).said_old})
    return kept


def _named(edit: Edit, both: list[Edit]) -> Edit:
    """Samples named agree with 'all' when they are every sample; the
    named ones are kept, else ground finds 'all' against the samples the
    user named."""
    if edit.rows != 'all':
        return edit
    named = next((e for e in both if e.rows != 'all'), None)
    return edit if named is None else edit.model_copy(update={'rows': named.rows})


def _first_per_said_old(edits: list[Edit]) -> dict[str, Edit]:
    said: dict[str, Edit] = {}
    for edit in edits:
        if edit.said_old and edit.said_old.strip():
            # by amount and unit: '2,000' and '2000.0' are one, '2 min' and
            # '2 h' are not
            number = value(edit.said_old)
            key = (
                f'{number.value.normalize():f} {number.unit or ""}'
                if number
                else edit.said_old.strip().casefold()
            )
            said.setdefault(key, edit)
    return said


def _first_per_anchor(edits: list[Edit]) -> dict[tuple[str, str], Edit]:
    pointed: dict[tuple[str, str], Edit] = {}
    for edit in edits:
        if edit.anchor:
            key = (edit.anchor.column, edit.anchor.equals.strip())
            pointed.setdefault(key, edit)
    return pointed


def _rows_agreement_key(rows: RowSpec, catalog: Catalog) -> RowsAgreementKey:  # noqa: PLR0911
    """Samples listed, a range and 'all' are the same rows when they name
    the same samples ("samples 1 to 13" is every sample when there is no
    6); the sheet decides, through the one row resolver. 'where' stays
    what it is: the rows holding a value are another reading than the
    samples that hold it now. Except on the samples' own labels: "where
    Subbatch = 2" is subbatch 2, whatever the notes change."""
    if isinstance(rows, Samples):
        try:
            return frozenset(resolve_rows(rows, catalog)[0])
        except Refusal:  # a label the sheet holds twice, no subbatch said
            return ('samples', tuple(rows.samples), rows.subbatch)
    if isinstance(rows, Range):
        try:
            return frozenset(resolve_rows(rows, catalog)[0])
        except Refusal:  # "samples 3 to 1", or the same repeated label
            return ('range', rows.first, rows.last, rows.subbatch)
    if rows == 'all':
        return frozenset(resolve_rows(rows, catalog)[0])
    if _on_labels(rows.where, catalog):
        try:
            return frozenset(resolve_rows(rows, catalog)[0])
        except Refusal:  # no sample holds it
            pass
    return ('where', rows.where, None if rows.equals is None else rows.equals.strip())


def _on_labels(column_id: str, catalog: Catalog) -> bool:
    """The column the catalog reads the sample or subbatch labels from, in
    the leading Experiment Info: a numbered step may have its own
    'Sample' value column."""
    column = catalog.column(column_id)
    if column is None or column.column.section.position is not None:
        return False
    return column.column.index in {
        info_column(catalog.structure.columns, header)
        for header in (SAMPLE_HEADER, SUBBATCH_HEADER)
    }


def _quote(said: str) -> str:
    """Punctuation at the edges, spacing and case are no disagreement."""
    return ' '.join(quote_words(said)).casefold()
