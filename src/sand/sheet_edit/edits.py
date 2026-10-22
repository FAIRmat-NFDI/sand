"""What the LLM may answer, and what becomes of each proposal.

The models below take any string. answer_model(catalog) narrows them to
one sheet: ids and sample labels become enums, so with constrained output
the LLM cannot name a column, formula column or sample that does not
exist. Values stay text, as the table shows them; checks convert them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from sand.sheet_edit.catalog import Catalog

Status = Literal['ok', 'unresolved', 'mismatch', 'uncertain', 'noop']


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Samples(_Model):
    """{"samples": ["1", "2", "3"], "subbatch": null}"""

    samples: list[str] = Field(min_length=1)
    subbatch: str | None  # needed when every subbatch restarts at 1


class Range(_Model):
    """{"first": "1", "last": "25", "subbatch": null}; code expands it."""

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
    rows: RowSpec
    anchor: Anchor | None
    old: str | None  # what the LLM believes is in the cell; None: empty
    new: str  # as the user said it, not converted
    said: str  # the user's words this edit came from


class Unresolved(_Model):
    reason: Literal['ambiguous', 'not_found']
    candidates: list[str]  # column ids the words could mean
    said: str


class Answer(_Model):
    edits: list[Edit]
    unresolved: list[Unresolved]


class EditResult(_Model):
    status: Status
    edit: Edit | None = None
    unresolved: Unresolved | None = None
    reasons: tuple[str, ...] = ()  # why it is not ok, as user messages


def answer_model(catalog: Catalog) -> type[Answer]:
    """Answer narrowed to this sheet; validates and gives the JSON schema."""
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
    unresolved = create_model(
        'Unresolved', __base__=Unresolved, candidates=(list[ids], ...)
    )
    return create_model(
        'Answer',
        __base__=Answer,
        edits=(list[edit], ...),
        unresolved=(list[unresolved], ...),
    )


def answer_schema(catalog: Catalog) -> dict:
    return answer_model(catalog).model_json_schema()


def _one_of(what: str, values: list[str]):
    if not values:
        raise ValueError(f'the sheet has no {what}: nothing can be edited')
    return Literal[tuple(values)]
