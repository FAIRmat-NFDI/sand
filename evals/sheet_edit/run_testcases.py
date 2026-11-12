"""Measure the sheet editor against the real model.

Every case of testcases.json, on the synthetic sheet, is run through `interpret_instruction`,
the function the edit job runs, with the model answering the two passes
the way the job's worker asks it (nomad-llm-extraction's engine, the
same prompt, schema and temperature, the same retries). What comes out
is compared with what the case expects: the edits (column, samples,
value, and whether a word check must mark it) that must be ready, the columns that must be flagged (refused or
left unresolved, never written), the unresolved kinds that must be said.

    ~/fairmat/nomad-distro-dev/.venv/bin/python \\
        evals/sheet_edit/run_testcases.py [--only ID] [--model gemini/...]

Run with the distribution's Python: the engine is installed there, and
sand too (editable, so this checkout's code runs). Not `uv run` in the
distribution, which may sync it. The provider's key comes from the shell
(GEMINI_API_KEY, read by the engine, never by this script). Every answer is kept as cache/<model>/<case id>.<pass>.json,
so a rerun only asks the questions whose prompt changed. Never run in CI.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'tests' / 'sheet_edit'))  # the synthetic sheet

from synthetic_sheet import build as synthetic  # noqa: E402

from sand.sheet_edit.apply import (  # noqa: E402
    Interpretation,
    Resolve,
    interpret_instruction,
)
from sand.sheet_edit.catalog import Catalog, build  # noqa: E402
from sand.sheet_edit.edits import Unreadable  # noqa: E402
from sand.sheet_edit.ground import Grounded, Refused, same_value  # noqa: E402
from sand.sheet_edit.resolve import (  # noqa: E402
    PROMPT_VERSION,
    agree,
    prompts,
    read_answer,
)

TEMPERATURE = 0  # the job's (actions/edit/workflows.py)
MAX_RETRIES = 3  # the extraction workflow's max_retry_attempts
GAVE_UP = 'Max retry attempts reached.'  # its message after the last retry
INFO = 'Experiment Info'

# the plan's gate before KIT uses it
TARGETS = {'precision': 99, 'flagged': 100, 'proposed': 95, 'refusals': 100}


# --- the cases ----------------------------------------------------------------


@dataclass(frozen=True)
class Expected:
    """One expected edit, resolved on the sheet: Excel rows and the value
    as said ('' clears). `check` True: the words contradict the sheet, so
    the right edit must carry a check (a warning on the edit), or be
    flagged. False: it must carry none. None: either."""

    target: str
    name: str  # "Rotation speed (2: Spin Coating)"
    rows: tuple[int, ...]
    new: str
    check: bool | None = None


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
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f'case ids repeat: {repeated}')
    return cases


def column_id(step: int | str, column: str, catalog: Catalog) -> str:
    """The id of `column` in `step`, by the sheet's own words: the step's
    number (or "Experiment Info") and the header without its unit."""

    def in_step(label: str) -> bool:
        return label == INFO if step == INFO else label.startswith(f'{step}:')

    found = [
        c.id
        for c in catalog.columns
        if in_step(c.column.section.label) and _header(c.column.header) == column
    ]
    if len(found) != 1:
        raise ValueError(f'{column!r} in step {step!r}: {len(found)} columns match')
    return found[0]


def _header(header: str) -> str:
    return header.split('[', maxsplit=1)[0].strip()


def rows_of(rows: str | list[str], catalog: Catalog) -> tuple[int, ...]:
    if rows == 'all':
        return tuple(s.row for s in catalog.samples)
    if isinstance(rows, str):
        raise ValueError(f'rows must be "all" or a list of sample labels, not {rows!r}')
    by_label = {s.label: s.row for s in catalog.samples}
    missing = [r for r in rows if r not in by_label]
    if missing:
        raise ValueError(f'no sample {missing} on the sheet')
    return tuple(sorted(by_label[r] for r in rows))


def _expected(raw: dict, catalog: Catalog) -> Expected:
    target = column_id(raw['step'], raw['column'], catalog)
    return Expected(
        target,
        _name(target, catalog),
        rows_of(raw['rows'], catalog),
        raw['new'],
        raw.get('check'),
    )


def _name(target: str, catalog: Catalog) -> str:
    column = next(c for c in catalog.columns if c.id == target)
    return f'{_header(column.column.header)} ({column.column.section.label})'


# --- the judgement --------------------------------------------------------------


@dataclass
class Verdict:
    """How one case went. `proposed`/`flagged`/`missed` count the expected
    edits; `invented` and `collateral` are ready edits nobody asked for
    (another column; the right column with other rows or value);
    `written` is a flagged column that got a ready edit, the worst
    outcome; `failed` is set when the job itself would have failed.
    `checked` and `wrong_checked`: of the right and of the wrong ready
    edits, those a word check marked, which the user sees warned.
    `check_missed`: right edits the case wants marked that came unmarked,
    written without a warning; `held` of the flagged, those the case
    wants marked, safe as well. `check_extra`: right edits marked that
    the case wants clean, one click too many."""

    case: Case
    proposed: int = 0
    flagged: int = 0
    missed: int = 0
    invented: int = 0
    collateral: int = 0
    written: int = 0
    checked: int = 0
    wrong_checked: int = 0
    check_missed: int = 0
    check_extra: int = 0
    held: int = 0
    refused_ok: bool = True  # the unresolved kinds asked for were said
    failed: str = ''
    notes: list[str] = field(default_factory=list)
    preview: str = ''

    @property
    def ok(self) -> bool:
        """Every asked edit proposed as asked, nothing else written, the
        refusals asked for said. A flagged edit is safe, not a pass."""
        wrong = self.flagged > self.held or self.missed or self.invented
        wrong = wrong or self.collateral or self.written
        wrong = wrong or self.check_missed or self.check_extra
        return not wrong and self.refused_ok and not self.failed


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
            if hit.checks and want.check is False:
                v.checked += 1
                v.check_extra += 1
                v.notes.append(f'{want.name}: proposed with a check it should not have')
            elif hit.checks:
                v.checked += 1
                v.notes.append(f'{want.name}: proposed with a check')
            elif want.check:
                v.check_missed += 1
                v.notes.append(f'{want.name}: proposed without the check it needs')
        elif any(g.edit.target == want.target for g in ready):
            # the column is right, the rows or the value are not: the
            # ready edit is counted below, the asked one is missed
            v.missed += 1
            v.notes.append(f'{want.name}: proposed with other rows or value')
        elif (
            want.target in refused
            or _unresolved_about(want.target, result)
            # what it points at is not there: a not-found can only be about
            # this edit when the case asks for no other
            or (want.check and len(case.edits) == 1 and 'not_found' in kinds)
        ):
            v.flagged += 1
            if want.check:
                v.held += 1
                v.notes.append(f'{want.name}: flagged, as safe as a check')
            else:
                v.notes.append(f'{want.name}: flagged, not proposed')
        else:
            v.missed += 1
            v.notes.append(f'{want.name}: nothing proposed')
    _count_unasked(v, ready, matched, result)
    silent = _dropped_in_silence(case, result, refused | {g.edit.target for g in ready})
    if silent:
        v.refused_ok = False
        v.notes += [
            f'{_name(t, result.catalog)}: not flagged, nothing said' for t in silent
        ]
    if case.unresolved and not kinds & set(case.unresolved):
        v.refused_ok = False
        v.notes.append(
            f'expected {" or ".join(case.unresolved)}, got '
            + (', '.join(sorted(kinds)) if kinds else 'no unresolved item')
        )
    return v


def _count_unasked(
    v: Verdict, ready: list[Grounded], matched: list[Grounded], result: Interpretation
) -> None:
    """Ready edits no expected edit matched: written although flagged,
    collateral (an asked column, other rows or value), or invented."""
    case = v.case
    wanted = {w.target for w in case.edits}
    for g in ready:
        if g in matched:
            continue
        name = _name(g.edit.target, result.catalog)
        if g.checks:
            v.wrong_checked += 1
            name += ' (with a check)'
        if g.edit.target in case.flagged:
            v.written += 1
            v.notes.append(f'{name}: WRITTEN, must be flagged')
        elif g.edit.target in wanted:
            v.collateral += 1
            v.notes.append(f'{name}: other rows or value ({_value(g)!r})')
        else:
            v.invented += 1
            v.notes.append(f'{name}: not asked for ({_value(g)!r})')


def _dropped_in_silence(
    case: Case, result: Interpretation, seen: set[str]
) -> list[str]:
    """Flagged columns neither refused, written nor said. With no edit
    asked, a not-found is about this one."""
    kinds = {u.reason for u in result.proposal.unresolved}
    if not case.edits and 'not_found' in kinds:
        return []
    return [
        t for t in case.flagged if t not in seen and not _unresolved_about(t, result)
    ]


def _unresolved_about(target: str, result: Interpretation) -> bool:
    """An unresolved item that covers this column: an unreadable answer
    covers everything; uncertain readings and ambiguous candidates name
    their columns. A not-found about other words covers nothing."""
    for u in result.proposal.unresolved:
        if u.reason == 'unreadable':
            return True
        if u.reason == 'uncertain' and any(r.target == target for r in u.readings):
            return True
        if u.reason == 'ambiguous' and target in u.candidates:
            return True
    return False


def _same(got: Grounded, want: Expected) -> bool:
    if tuple(sorted(got.rows)) != want.rows:
        return False
    if want.new == '':
        return got.value is None
    if got.value is None:
        return False
    if isinstance(got.value, str):
        # text the way the job accepts it: case and spacing aside
        return _norm(got.value) == _norm(want.new)
    return same_value(want.new, got.value)


def _norm(text: str) -> str:
    return ' '.join(text.split()).casefold()


def _value(g: Grounded) -> object:
    return '' if g.value is None else g.value


# --- the numbers ----------------------------------------------------------------


def summarize(verdicts: list[Verdict]) -> dict[str, object]:
    asked = sum(len(v.case.edits) for v in verdicts)
    proposed = sum(v.proposed for v in verdicts)
    flagged = sum(v.flagged for v in verdicts)
    wrong = sum(v.invented + v.collateral + v.written for v in verdicts)
    refusals_asked = [v for v in verdicts if v.case.unresolved or v.case.flagged]
    refusals_ok = [
        v for v in refusals_asked if v.refused_ok and not v.written and not v.failed
    ]
    return {
        'cases': len(verdicts),
        'passed': sum(v.ok for v in verdicts),
        'failed': sum(bool(v.failed) for v in verdicts),
        'precision': _percent(proposed, proposed + wrong),
        'proposed': _percent(proposed, asked),
        'flagged': _percent(proposed + flagged, asked),
        'refusals': _percent(len(refusals_ok), len(refusals_asked)),
        'invented': sum(v.invented for v in verdicts),
        'collateral': sum(v.collateral for v in verdicts),
        'written': sum(v.written for v in verdicts),
        'checked': sum(v.checked for v in verdicts),
        'wrong_checked': sum(v.wrong_checked for v in verdicts),
        'check_asked': sum(bool(w.check) for v in verdicts for w in v.case.edits),
        'check_missed': sum(v.check_missed for v in verdicts),
        'check_extra': sum(v.check_extra for v in verdicts),
        # wrong edits shown without a warning: the one number to keep at 0
        'wrong_unchecked': wrong - sum(v.wrong_checked for v in verdicts),
    }


def _percent(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(100 * part / whole, 1)


def report(verdicts: list[Verdict]) -> str:
    lines = []
    for v in verdicts:
        lines.append(f'{v.case.id:<18} {"ok" if v.ok else "FAIL"}')
        for note in v.notes:
            lines.append(f'{"":<18}   {note}')
        if not v.ok:
            lines.extend(f'{"":<18}   | {line}' for line in v.preview.splitlines())
    s = summarize(verdicts)
    lines.append('')
    passed = f'{s["passed"]}/{s["cases"]} cases pass'
    if s['failed']:
        passed += f' · {s["failed"]} would fail the job'
    lines.append(passed)
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
    lines.append(
        f'with a check: {s["checked"]} right ({s["check_asked"]} asked for), '
        f'{s["wrong_checked"]} wrong · check missing {s["check_missed"]}, '
        f'not wanted {s["check_extra"]} · wrong without a check {s["wrong_unchecked"]}'
    )
    return '\n'.join(lines)


# --- the model ------------------------------------------------------------------


class ModelGaveUp(Exception):
    """No answer fit the schema in MAX_RETRIES: the worker's
    'Max retry attempts reached.', an unreadable opinion."""


class JobWouldFail(Exception):
    """What fails the whole job on the worker: an empty answer, an answer
    that is not JSON, the provider unreachable."""


# (the slot's name, system prompt, user text, schema) -> the answer text
Ask = Callable[[str, str, str, dict], str]
PASSES = {False: 'first', True: 'second'}  # resolve.py's two opinions


def cached_model(model: str, cache: Path) -> Ask:
    """The job's way of asking: the extraction library's engine, the two
    prompts joined as the worker joins them, temperature 0, the answer
    parsed and checked against the schema, the worker's retries with its
    wording. Each answer, or giving up, is kept as
    cache/<model>/<case id>.<pass>.json, reused while its model,
    instruction and PROMPT_VERSION (bumped on every prompt change) are
    the same."""
    from nomad_llm_extraction.pipeline.schema_filling.llm_engine import LiteLLMEngine
    from nomad_llm_extraction.utils.utils import validate_with_schema

    engine = LiteLLMEngine(model_name=model)
    folder = cache / model.rpartition('/')[2]
    folder.mkdir(parents=True, exist_ok=True)

    def call(name: str, system: str, text: str, schema: dict) -> str:
        prompt = f'{system}\n\n{text}'
        said = text.rpartition('Instruction: ')[2]
        slot = folder / f'{name}.json'
        kept = reusable(slot, model, said)
        if kept is not None:
            if kept.get('error'):
                raise ModelGaveUp(kept['error'])
            return json.dumps(kept['answer'])
        retry = ''
        raw = ''
        for _ in range(MAX_RETRIES):
            try:
                # a copy: litellm rewrites the schema in place for Gemini
                # (anyOf null -> nullable), which would fail every null
                # in the answer below. The worker's activities each get
                # their own copy through Temporal.
                raw = engine.generate(
                    prompt + retry, copy.deepcopy(schema), {'temperature': TEMPERATURE}
                )
            except Exception as why:
                raise JobWouldFail(f'the model call failed: {why}') from why
            if not raw:
                raise JobWouldFail('the model answered nothing')
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as why:
                raise JobWouldFail(f'the answer is not JSON: {why}') from why
            valid, message = validate_with_schema(data, schema)
            if valid:
                keep(slot, model, said, answer=data)
                return json.dumps(data)
            retry += (
                f'\n\nPrevious attempt output:\n{raw}\n\nNote: The previous '
                f'attempt resulted in an error: ValidationError: {message}. '
                'Please try again and ensure the output adheres to the schema.'
            )
        keep(slot, model, said, error=GAVE_UP, raw=raw)
        raise ModelGaveUp(GAVE_UP)

    return call


def reusable(slot: Path, model: str, said: str) -> dict | None:
    """The kept answer, if it was asked of this model, with this
    instruction and these prompts."""
    if not slot.exists():
        return None
    kept = json.loads(slot.read_text())
    same = (kept.get('model'), kept.get('instruction'), kept.get('prompt_version'))
    return kept if same == (model, said, PROMPT_VERSION) else None


def keep(slot: Path, model: str, said: str, **kept: object) -> None:
    """One cached answer, readable: the instruction, the answer as JSON."""
    head = {'model': model, 'prompt_version': PROMPT_VERSION, 'instruction': said}
    if isinstance(kept.get('raw'), str):
        try:  # a failed answer that is still JSON reads as JSON
            kept['raw'] = json.loads(kept['raw'])
        except json.JSONDecodeError:
            pass
    slot.write_text(json.dumps({**head, **kept}, indent=2, ensure_ascii=False) + '\n')


def asking(call: Ask, case_id: str) -> Resolve:
    """The worker's resolve for one case: the two prompts, each answer
    read, a model that gave up an unreadable opinion, the opinions agreed."""

    def ask(text: str, catalog: Catalog):
        opinions = []
        for p in prompts(text, catalog):
            name = f'{case_id}.{PASSES[p.second_opinion]}'
            try:
                answer = call(name, p.system, p.text, p.schema)
            except ModelGaveUp as why:
                opinions.append(
                    Unreadable(reason='unreadable', detail=str(why), said=text)
                )
            else:
                opinions.append(read_answer(answer, text, catalog))
        return agree(opinions, catalog)

    return ask


def run(
    cases: list[Case], sheet: bytes, call: Ask, only: str | None = None
) -> list[Verdict]:
    chosen = [c for c in cases if only is None or c.id == only]
    if not chosen:
        raise ValueError(f'no case {only!r}')
    verdicts = []
    for number, case in enumerate(chosen, 1):
        # progress on stderr: a case is two model calls, a few seconds
        print(f'{number}/{len(chosen)} {case.id}', file=sys.stderr, flush=True)
        ask = asking(call, case.id)
        try:
            verdicts.append(judge(case, interpret_instruction(case.text, sheet, ask)))
        except JobWouldFail as why:
            v = Verdict(case, missed=len(case.edits), failed=str(why))
            v.refused_ok = not case.unresolved
            v.notes.append(f'the job would fail: {why}')
            verdicts.append(v)
    return verdicts


def configured_model() -> str:
    """The job's model, from the distribution's nomad.yaml: this runs on
    the distribution's .venv, whose parent folder holds it."""
    distro = Path(sys.prefix).parent / 'nomad.yaml'
    if 'NOMAD_CONFIG' not in os.environ and distro.exists():
        os.environ['NOMAD_CONFIG'] = str(distro)
    from nomad.config import config

    from sand.actions.common import llm_model_name

    config.load_plugins()  # NOMAD loads them at startup, a script must ask
    return llm_model_name()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--model', help="default: the job's, from nomad.yaml")
    parser.add_argument('--only', help='run this one case')
    args = parser.parse_args(argv)

    sheet = synthetic()
    # the sheet the cases are asked about, to open next to the answers
    (HERE / 'cache').mkdir(exist_ok=True)
    (HERE / 'cache' / 'synthetic_sheet.xlsx').write_bytes(sheet)
    cases = load_cases(HERE / 'testcases.json', build(sheet))
    if args.only and all(c.id != args.only for c in cases):
        parser.error(f'no case {args.only!r}; ids: {", ".join(c.id for c in cases)}')
    model = args.model or configured_model()
    print(f'model {model}', file=sys.stderr, flush=True)
    verdicts = run(cases, sheet, cached_model(model, HERE / 'cache'), args.only)
    print(report(verdicts))
    return 0 if all(v.ok for v in verdicts) else 1


if __name__ == '__main__':
    sys.exit(main())
