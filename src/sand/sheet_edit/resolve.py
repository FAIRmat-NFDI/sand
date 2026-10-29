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
agree on go on as edits; the rest becomes `Uncertain`, shown with every
reading and never applied without the user choosing. Same model means
shared blind spots: this is one more layer, the deterministic checks in
ground.py are the safety net.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.coverage import quote_words
from sand.sheet_edit.edits import (
    Edit,
    ProposedEdits,
    Range,
    RowSpec,
    Samples,
    Uncertain,
    Unreadable,
    Unresolved,
    proposed_edits_model,
    proposed_edits_schema,
)
from sand.sheet_edit.ground import Refusal, resolve_rows
from sand.sheet_edit.render import table

# recorded with every proposal (changelog, status); bump on any change to
# the prompts below or to the table in render.py, which the model reads
PROMPT_VERSION = 'v1'

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
- "new": the value as the user said it. Never convert units, scale or round. \
To clear a cell, "new" is "" (the user said to clear, empty or remove it); \
(empty) is never a value.
- "old": the current value the table shows for the cell, without the quotes; \
null when the cell is empty.
- "rows": "all" when the user names no samples; the samples named; a range \
("samples 1 to 3"); or "where" another column holds a value, to pick the \
rows by it ("the samples with variation 20min": where = that column, \
equals = "20min").
- "anchor": when the user names a value in a neighbouring column of the \
same step to say which cell they mean ("the volume of the DMSO": the \
volume column is the target, anchor = the solvent column, equals = "DMSO").
- A value the user mentions as current ("the 3000") is a clue to the cell: \
use it as "old".
- If the words fit more than one column, do not pick one: answer \
"ambiguous" with every candidate.
- If what the user points at is not in the table, answer "not_found".
- One edit per column and group of samples. Nothing the user asked for may \
be dropped: what you cannot place goes to "unresolved"."""

_RULES_B = """\
How to answer:
- Every edit must come from the user's words and nothing else. Never invent \
a value, a sample or a column the user did not say.
- Copy the user's exact words for this edit into "said", unchanged.
- Put the value in "new" exactly as spoken: no unit conversion, no \
rounding, no rescaling. Clearing a cell (the user says clear, empty, \
remove) is "new": ""; the marker (empty) is not a value.
- "old" is the current value shown in the table for that cell, quotes \
left out, or null for an empty cell.
- Choose "rows" as "all" if no sample is named, else the named samples, a \
"first"/"last" range for "samples 1 to 3", or "where" when the user picks \
rows by what another column holds ("the samples with variation 20min").
- Use "anchor" when the user identifies the cell by a value next to it in \
the same step ("the volume of the DMSO" edits the volume column; the \
anchor is the solvent column with equals "DMSO").
- When the user quotes a current value ("the 3000"), it identifies the \
cell: it is the "old" value.
- Several columns could be meant? Then "ambiguous" with all of them, never \
a guess.
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
    left the same words unresolved. Every other edit is uncertain, one
    item per quote with every reading of it. Unresolved items of either
    pass are kept once."""
    keys_a: dict[AgreementKey, Edit] = {}
    keys_b: dict[AgreementKey, Edit] = {}
    for edit in a.edits:
        keys_a.setdefault(
            _agreement_key(edit, catalog), edit
        )  # a repeat differs in `old` only
    for edit in b.edits:
        keys_b.setdefault(_agreement_key(edit, catalog), edit)
    hedged = {_quote(u.said) for u in [*a.unresolved, *b.unresolved]}
    edits: list[Edit] = []
    uncertain: dict[str, Uncertain] = {}  # by quote, in order of appearance

    def doubt(edit: Edit, why: str) -> None:
        quote = _quote(edit.said)
        if quote in uncertain:
            item = uncertain[quote]
            uncertain[quote] = item.model_copy(
                update={'readings': [*item.readings, edit], 'why': _DIFFER}
            )
        else:
            uncertain[quote] = Uncertain(
                reason='uncertain', readings=[edit], why=why, said=edit.said
            )

    for key, edit in keys_a.items():
        if key not in keys_b:
            doubt(edit, _ONE_PASS)
        elif hedged & {_quote(edit.said), _quote(keys_b[key].said)}:
            # the passes may cut the quote differently: a hedge on either
            # pass's quote counts
            doubt(edit, _HEDGED)
        else:
            edits.append(edit)
    for key, edit in keys_b.items():
        if key not in keys_a:
            doubt(edit, _ONE_PASS)
    seen: set[tuple] = set()
    unresolved: list[Unresolved] = []
    for item in [*a.unresolved, *b.unresolved]:
        key = (
            item.reason,
            _quote(item.said),
            frozenset(getattr(item, 'candidates', ())),
        )
        if key not in seen:
            seen.add(key)
            unresolved.append(item)
    return ProposedEdits(edits=edits, unresolved=[*uncertain.values(), *unresolved])


_ONE_PASS = 'only one of the two passes proposed it'
_DIFFER = 'the two passes read these words differently'
_HEDGED = 'a pass also left these words unresolved'


# the rows of an edit, as compared between the two opinions: the sheet
# rows when the sample words resolve, else the spec as given
RowsAgreementKey = Literal['all'] | frozenset[int] | tuple
# (target, rows, anchor, new): what two opinions must agree on
AgreementKey = tuple[str, RowsAgreementKey, tuple[str, str] | None, str]


def _agreement_key(edit: Edit, catalog: Catalog) -> AgreementKey:
    """What two passes must agree on: the cells and the value. Left out:
    `old`, which ground checks against the sheet anyway, and `said`,
    which the passes may cut differently for one and the same edit and
    coverage checks against the instruction. The value is compared as
    written: material names are case-sensitive (PbI2, Me-4PACz)."""
    anchor = (edit.anchor.column, edit.anchor.equals.strip()) if edit.anchor else None
    return (
        edit.target,
        _rows_agreement_key(edit.rows, catalog),
        anchor,
        edit.new.strip(),
    )


def _rows_agreement_key(rows: RowSpec, catalog: Catalog) -> RowsAgreementKey:
    """Samples listed and the range of the same samples are the same rows;
    the sheet decides, through the one row resolver. 'all' and 'where'
    stay what they are: named samples and 'every sample' are two readings."""
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
        return 'all'
    return ('where', rows.where, None if rows.equals is None else rows.equals.strip())


def _quote(said: str) -> str:
    """Punctuation at the edges, spacing and case are no disagreement."""
    return ' '.join(quote_words(said)).casefold()
