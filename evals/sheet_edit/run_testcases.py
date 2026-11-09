"""Measure the sheet editor against the real model.

Every case of a testcases file is run through `interpret_instruction`,
the function the edit job runs, with the model answering the two passes
the way the job's worker asks it (nomad-llm-extraction's engine, the
same prompt, schema and temperature). What comes out is compared with
what the case expects: the edits (column, samples, value) that must be
ready, the columns that must be flagged (refused or left unresolved,
never written), the unresolved kinds that must be said.

    uv run --directory ~/fairmat/nomad-distro-dev python \\
        evals/sheet_edit/run_testcases.py [--model gemini/...] [--only ID] [--show]

Run from the distribution's environment: that is where the engine and
the provider's key live (GEMINI_API_KEY, read by the engine, never by
this script). Every model answer is cached under cache/ by model and
prompt, so a rerun after a prompt change only pays for the prompts that
changed. Never run in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'tests' / 'sheet_edit'))  # the synthetic sheet

from sand.sheet_edit.apply import Interpretation, interpret_instruction  # noqa: E402
from sand.sheet_edit.catalog import Catalog, build  # noqa: E402
from sand.sheet_edit.ground import Grounded, Refused, same_value  # noqa: E402
from sand.sheet_edit.resolve import CallLLM, resolve  # noqa: E402

DEFAULT_MODEL = 'gemini/gemini-2.5-flash'  # sand's default; pass nomad.yaml's
TEMPERATURE = 0  # the job's (actions/edit/workflows.py)
MAX_RETRIES = 3  # the extraction workflow's default
INFO = 'Experiment Info'

# the plan's gate before KIT uses it
TARGETS = {'precision': 99, 'flagged': 100, 'proposed': 95, 'refusals': 100}


# --- the cases ----------------------------------------------------------------


@dataclass(frozen=True)
class Expected:
    """One expected edit, resolved on the sheet: Excel rows and the value
    as `ground` would write it (None clears)."""

    target: str
    name: str  # "Rotation speed (2: Spin Coating)"
    rows: tuple[int, ...]
    new: str


@dataclass(frozen=True)
class Case:
    id: str
    text: str
    edits: tuple[Expected, ...]
    flagged: tuple[str, ...]  # column ids that must not be written
    unresolved: tuple[str, ...]  # one of these kinds must be said
    why: str = ''


def load_cases(path: Path, catalog: Catalog) -> list[Case]:
    data = json.loads(path.read_text())
    cases = []
    for raw in data['cases']:
        expect = raw.get('expect', {})
        cases.append(
            Case(
                id=raw['id'],
                text=raw['text'],
                edits=tuple(_expected(e, catalog) for e in expect.get('edits', [])),
                flagged=tuple(
                    column_id(f['step'], f['column'], catalog)
                    for f in expect.get('flagged', [])
                ),
                unresolved=tuple(expect.get('unresolved', [])),
                why=raw.get('why', ''),
            )
        )
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(
            f'case ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}'
        )
    return cases


def column_id(step: int | str, column: str, catalog: Catalog) -> str:
    """The id of `column` in `step`, by the sheet's own words: the step's
    number (or "Experiment Info") and the header without its unit."""
    section = INFO if step == INFO else f'{step}:'
    found = [
        c.id
        for c in catalog.columns
        if (
            c.column.section.label == INFO
            if section == INFO
            else c.column.section.label.startswith(section)
        )
        and _header(c.column.header) == column
    ]
    if len(found) != 1:
        raise ValueError(f'{column!r} in step {step!r}: {len(found)} columns match')
    return found[0]


def _header(header: str) -> str:
    return header.split('[', maxsplit=1)[0].strip()


def rows_of(rows: str | list[str], catalog: Catalog) -> tuple[int, ...]:
    if rows == 'all':
        return tuple(s.row for s in catalog.samples)
    by_label = {s.label: s.row for s in catalog.samples}
    missing = [r for r in rows if r not in by_label]
    if missing:
        raise ValueError(f'no sample {missing} on the sheet')
    return tuple(sorted(by_label[r] for r in rows))


def _expected(raw: dict, catalog: Catalog) -> Expected:
    target = column_id(raw['step'], raw['column'], catalog)
    column = next(c for c in catalog.columns if c.id == target)
    name = f'{_header(column.column.header)} ({column.column.section.label})'
    return Expected(target, name, rows_of(raw['rows'], catalog), raw['new'])


# --- the judgement --------------------------------------------------------------


@dataclass
class Verdict:
    """How one case went. `proposed`/`flagged`/`missed` count the expected
    edits; `invented` and `collateral` are ready edits nobody asked for
    (a wrong value, wrong rows); `written` is a flagged column that got
    a ready edit, the worst outcome."""

    case: Case
    proposed: int = 0
    flagged: int = 0
    missed: int = 0
    invented: int = 0
    collateral: int = 0
    written: int = 0
    refused_ok: bool = True  # the unresolved kinds asked for were said
    notes: list[str] = field(default_factory=list)
    preview: str = ''

    @property
    def ok(self) -> bool:
        """Every asked edit proposed as asked, nothing else written, the
        refusals asked for said. A flagged edit is safe, not a pass."""
        wrong = self.flagged or self.missed or self.invented or self.collateral
        return not wrong and not self.written and self.refused_ok


def judge(case: Case, result: Interpretation) -> Verdict:
    v = Verdict(case, preview=result.preview)
    ready = list(result.ready.values())
    refused = {r.edit.target for r in result.results if isinstance(r, Refused)}
    kinds = {u.reason for u in result.proposal.unresolved}
    matched: list[Grounded] = []
    for want in case.edits:
        hit = next(
            (g for g in ready if g.edit.target == want.target and _same(g, want)),
            None,
        )
        if hit:
            v.proposed += 1
            matched.append(hit)
        elif any(g.edit.target == want.target for g in ready):
            # the column is right, the rows or the value are not: the
            # ready edit is counted below, the asked one is missed
            v.missed += 1
            v.notes.append(f'{want.name}: proposed with other rows or value')
        elif want.target in refused or kinds:
            v.flagged += 1
            v.notes.append(f'{want.name}: flagged, not proposed')
        else:
            v.missed += 1
            v.notes.append(f'{want.name}: nothing proposed')
    wanted = {w.target for w in case.edits}
    for g in ready:
        if g in matched:
            continue
        name = _name(g, result.catalog)
        if g.edit.target in case.flagged:
            v.written += 1
            v.notes.append(f'{name}: WRITTEN, must be flagged')
        elif g.edit.target in wanted:
            v.collateral += 1
            v.notes.append(f'{name}: other rows or value ({_value(g)!r})')
        else:
            v.invented += 1
            v.notes.append(f'{name}: not asked for ({_value(g)!r})')
    if case.unresolved and not kinds & set(case.unresolved):
        v.refused_ok = False
        v.notes.append(
            f'expected {" or ".join(case.unresolved)}, got '
            + (', '.join(sorted(kinds)) if kinds else 'no unresolved item')
        )
    return v


def _same(got: Grounded, want: Expected) -> bool:
    if tuple(sorted(got.rows)) != want.rows:
        return False
    if want.new == '':
        return got.value is None
    return got.value is not None and same_value(want.new, got.value)


def _value(g: Grounded) -> object:
    return '' if g.value is None else g.value


def _name(g: Grounded, catalog: Catalog) -> str:
    column = next(c for c in catalog.columns if c.id == g.edit.target)
    return f'{_header(column.column.header)} ({column.column.section.label})'


# --- the numbers ----------------------------------------------------------------


def summarize(verdicts: list[Verdict]) -> dict[str, object]:
    asked = sum(len(v.case.edits) for v in verdicts)
    proposed = sum(v.proposed for v in verdicts)
    flagged = sum(v.flagged for v in verdicts)
    wrong = sum(v.invented + v.collateral + v.written for v in verdicts)
    refusals_asked = [v for v in verdicts if v.case.unresolved or v.case.flagged]
    refusals_ok = [v for v in refusals_asked if v.refused_ok and not v.written]
    return {
        'cases': len(verdicts),
        'passed': sum(v.ok for v in verdicts),
        'precision': _percent(proposed, proposed + wrong),
        'proposed': _percent(proposed, asked),
        'flagged': _percent(proposed + flagged, asked),
        'refusals': _percent(len(refusals_ok), len(refusals_asked)),
        'invented': sum(v.invented for v in verdicts),
        'collateral': sum(v.collateral for v in verdicts),
        'written': sum(v.written for v in verdicts),
    }


def _percent(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(100 * part / whole, 1)


def report(verdicts: list[Verdict], show: bool) -> str:
    lines = []
    for v in verdicts:
        lines.append(f'{v.case.id:<18} {"ok" if v.ok else "FAIL"}')
        for note in v.notes:
            lines.append(f'{"":<18}   {note}')
        if show or not v.ok:
            lines.extend(f'{"":<18}   | {line}' for line in v.preview.splitlines())
    s = summarize(verdicts)
    lines.append('')
    lines.append(f'{s["passed"]}/{s["cases"]} cases pass')
    numbers = []
    for key in ('precision', 'proposed', 'flagged', 'refusals'):
        value = s[key]
        text = '–' if value is None else f'{value} %'
        gate = TARGETS[key]
        mark = '' if value is None or value >= gate else f' (target {gate} %)'
        numbers.append(f'{key} {text}{mark}')
    lines.append(' · '.join(numbers))
    lines.append(
        f'invented {s["invented"]} · collateral {s["collateral"]} · '
        f'written although flagged {s["written"]}'
    )
    return '\n'.join(lines)


# --- the model ------------------------------------------------------------------


def cached_model(model: str, cache: Path) -> CallLLM:
    """The job's way of asking: the extraction library's engine, the two
    prompts joined as the worker joins them, temperature 0, the answer
    parsed and checked against the schema, a few retries with the error
    quoted back. Answers are kept on disk by model and prompt."""
    from jsonschema import ValidationError, validate
    from nomad_llm_extraction.pipeline.schema_filling.llm_engine import LiteLLMEngine

    engine = LiteLLMEngine(model_name=model)
    cache.mkdir(parents=True, exist_ok=True)

    def call(system: str, text: str, schema: dict) -> str:
        prompt = f'{system}\n\n{text}'
        key = hashlib.sha256(
            json.dumps([model, prompt, schema], sort_keys=True).encode()
        ).hexdigest()
        slot = cache / f'{key}.json'
        if slot.exists():
            return json.loads(slot.read_text())['answer']
        retry = ''
        raw = ''
        for _ in range(MAX_RETRIES):
            raw = engine.generate(prompt + retry, schema, {'temperature': TEMPERATURE})
            try:
                data = json.loads(raw)
                validate(data, schema)
            except (json.JSONDecodeError, ValidationError) as why:
                error = str(why).partition('\n')[0]
                retry += (
                    f'\n\nPrevious attempt output:\n{raw}\n\nNote: The previous '
                    f'attempt resulted in an error: {error}. Please try again and '
                    'ensure the output adheres to the schema.'
                )
                continue
            answer = json.dumps(data)
            slot.write_text(json.dumps({'model': model, 'answer': answer}))
            return answer
        # the worker reports this as unreadable; the raw text says why
        return raw

    return call


def run(
    cases: list[Case], sheet: bytes, call: CallLLM, only: str | None = None
) -> list[Verdict]:
    def ask(text, catalog):
        return resolve(text, catalog, call)

    chosen = [c for c in cases if only is None or c.id == only]
    return [judge(c, interpret_instruction(c.text, sheet, ask)) for c in chosen]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--cases', type=Path, default=HERE / 'testcases.json')
    parser.add_argument(
        '--sheet', type=Path, help='an xlsx; default: the synthetic sheet'
    )
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--cache', type=Path, default=HERE / 'cache')
    parser.add_argument('--only', help='one case id')
    parser.add_argument(
        '--show', action='store_true', help='print every preview, not only failed'
    )
    args = parser.parse_args(argv)

    if args.sheet:
        sheet = args.sheet.read_bytes()
    else:
        from synthetic_sheet import build as synthetic

        sheet = synthetic()
    cases = load_cases(args.cases, build(sheet))
    verdicts = run(cases, sheet, cached_model(args.model, args.cache), args.only)
    print(report(verdicts, args.show))
    return 0 if all(v.ok for v in verdicts) else 1


if __name__ == '__main__':
    sys.exit(main())
