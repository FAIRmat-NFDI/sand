"""What the LLM may propose, and what becomes of each proposal.

The models below take any string. proposed_edits_model(catalog) narrows them to
one sheet: ids and sample labels become enums, so with constrained output
the LLM cannot name a column, formula column or sample that does not
exist. Values stay text, as the table shows them; checks convert them.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from sand.sheet_edit.catalog import Catalog

Status = Literal['ok', 'unresolved', 'mismatch', 'uncertain', 'noop']
# computed from the checks (ground.py), never from the LLM's self-rating
Confidence = Literal['high', 'medium', 'low']


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Samples(_Model):
    """{"samples": ["1", "2", "3"], "subbatch": null}"""

    samples: list[str] = Field(min_length=1)
    # Null stays allowed even when subbatches restart at 1: forcing a value
    # would make the LLM guess one when the user named none. The rows check
    # refuses a label that is then found in more than one subbatch.
    subbatch: str | None


class Range(_Model):
    """{"first": "1", "last": "25", "subbatch": null} for "samples 1 to 25".

    The LLM only copies the two words it heard; code expands the range from
    the real samples. Listing 1..25 itself, the LLM could drop or add one,
    could not know sample 22 is missing, and check S could not match the
    list against the words said.
    """

    first: str
    last: str
    subbatch: str | None


class Where(_Model):
    """{"where": "C007", "equals": "15min"}: rows whose Variation is 15min."""

    where: str
    equals: str | None  # None: the rows where that column is empty


RowSpec = Literal['all'] | Samples | Range | Where


class Anchor(_Model):
    """The cell the user pointed through: "the volume of the DMSO" is
    {"column": <Solvent 2 name>, "equals": "DMSO"}."""

    column: str
    equals: str


class Edit(_Model):
    target: str  # column id
    rows: RowSpec  # 'all' | {samples} | {first, last} | {where, equals}
    anchor: Anchor | None
    old: str | None  # what the LLM believes is in the cell; None: empty
    new: str  # as the user said it, not converted
    said: str  # the user's words this edit came from


class Ambiguous(_Model):
    """The words fit more than one cell or row group."""

    reason: Literal['ambiguous']
    candidates: list[str] = Field(min_length=1)  # column ids the words could mean
    said: str


class NotFound(_Model):
    """What the user pointed at is not in the sheet."""

    reason: Literal['not_found']
    said: str


class Uncertain(_Model):
    """The two LLM passes did not agree on these words (resolve.py). Never
    in the LLM's schema: only code produces it. `readings` are the edits
    the passes made of them, `why` says how they disagreed."""

    reason: Literal['uncertain']
    readings: list[Edit] = Field(min_length=1)
    why: str
    said: str


class Unreadable(_Model):
    """An LLM answer that was not valid JSON for the schema. Never in the
    LLM's schema. `said` is the whole instruction: nothing of it is done."""

    reason: Literal['unreadable']
    detail: str
    said: str


Unresolved = Annotated[
    Ambiguous | NotFound | Uncertain | Unreadable, Field(discriminator='reason')
]


class ProposedEdits(_Model):
    edits: list[Edit]
    unresolved: list[Unresolved]


class EditResult(_Model):
    status: Status
    edit: Edit | None = None
    unresolved: Unresolved | None = None
    reasons: tuple[str, ...] = ()  # why it is not ok, as user messages
    confidence: Confidence | None = None  # for applied edits
    notes: tuple[str, ...] = ()
    doubts: tuple[str, ...] = ()  # low confidence: confirm each


def proposed_edits_model(catalog: Catalog) -> type[ProposedEdits]:
    """ProposedEdits narrowed to this sheet; validates and gives the JSON
    schema."""
    ids = _one_of('column ids', [c.id for c in catalog.columns])
    writable = _one_of(
        'writable columns', [c.id for c in catalog.columns if not c.formula]
    )
    labels = _one_of(
        'sample labels',
        list(dict.fromkeys(s.label for s in catalog.samples if s.label)),
    )
    subbatches = list(dict.fromkeys(s.subbatch for s in catalog.samples if s.subbatch))
    subbatch = _one_of('subbatches', subbatches) | None if subbatches else None

    samples = create_model(
        'Samples',
        __base__=Samples,
        samples=(list[labels], Field(min_length=1)),
        subbatch=(subbatch, ...),
    )
    range_ = create_model(
        'Range',
        __base__=Range,
        first=(labels, ...),
        last=(labels, ...),
        subbatch=(subbatch, ...),
    )
    where = create_model('Where', __base__=Where, where=(ids, ...))
    anchor = create_model('Anchor', __base__=Anchor, column=(ids, ...))
    edit = create_model(
        'Edit',
        __base__=Edit,
        target=(writable, ...),
        rows=(Literal['all'] | samples | range_ | where, ...),
        anchor=(anchor | None, ...),
    )
    ambiguous = create_model(
        'Ambiguous',
        __base__=Ambiguous,
        candidates=(list[ids], Field(min_length=1)),
    )
    unresolved = Annotated[ambiguous | NotFound, Field(discriminator='reason')]
    return create_model(
        'ProposedEdits',
        __base__=ProposedEdits,
        edits=(list[edit], ...),
        unresolved=(list[unresolved], ...),
    )


def proposed_edits_schema(catalog: Catalog) -> dict:
    return proposed_edits_model(catalog).model_json_schema()


def _one_of(what: str, values: list[str]):
    if not values:
        raise ValueError(f'the sheet has no {what}: nothing can be edited')
    return Literal[tuple(values)]
