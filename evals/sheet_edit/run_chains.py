"""Measure answer reuse against the real model.

Every chain of chains.json is a list of notes and the clicks made on
them (confirm, reject, undo a rejection, reword, delete, add). It is
played on the synthetic sheet the way the edit job runs it:
`apply_all`, in order, in turn, one job after every click. After each
job, every note that reused an answer read on another sheet is also
asked fresh on the sheet it is now read on. Both must make the same
ready edits: the same cells, the same values. That is the gate, 100 %.

Also counted: the readings the job asked for, the readings reuse saved,
and the notes read again although their earlier answer made the same
edits there (the price of being careful).

    ~/fairmat/nomad-distro-dev/.venv/bin/python \\
        evals/sheet_edit/run_chains.py [--only ID] [--model gemini/...]

Run it like run_testcases.py: with the distribution's Python, the key
from the shell (GEMINI_API_KEY). Every answer is kept under
cache/chains/<model>/, by the prompt it answers, so a rerun asks only
new questions. Never run in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / 'tests' / 'sheet_edit'))

import run_testcases as rt  # noqa: E402
from synthetic_sheet import build as synthetic  # noqa: E402

from sand.sheet_edit.apply import (  # noqa: E402
    Answer,
    Cache,
    Interpretation,
    Resolve,
    Step,
    apply_all,
    interpret_instruction,
)
from sand.sheet_edit.catalog import Catalog  # noqa: E402
from sand.sheet_edit.edits import Unreadable  # noqa: E402
from sand.sheet_edit.resolve import agree, prompts, read_answer  # noqa: E402
from sand.sheet_edit.workbook import sha256  # noqa: E402

# a click on a note, and whether it names one / brings words
CLICKS = {
    'confirm': (True, False),
    'reject': (True, False),
    'undo': (True, False),
    'reword': (True, True),
    'delete': (True, False),
    'add': (False, True),
}


# --- the chains -----------------------------------------------------------------


@dataclass(frozen=True)
class Click:
    do: str
    note: int | None = None  # 1-based, the note's number when it was added
    text: str | None = None


@dataclass(frozen=True)
class Chain:
    id: str
    notes: tuple[str, ...]
    clicks: tuple[Click, ...]
    why: str = ''


def load_chains(path: Path) -> list[Chain]:
    chains = []
    for raw in json.loads(path.read_text())['chains']:
        chain = Chain(
            id=raw['id'],
            notes=tuple(raw['notes']),
            clicks=tuple(_click(c, raw['id']) for c in raw['clicks']),
            why=raw.get('why', ''),
        )
        _check_notes(chain)
        chains.append(chain)
    ids = [c.id for c in chains]
    if len(set(ids)) != len(ids):
        raise ValueError(
            f'chain ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}'
        )
    return chains


def _click(raw: dict, chain: str) -> Click:
    do = raw.get('do')
    if do not in CLICKS:
        raise ValueError(f'{chain}: unknown click {do!r}')
    names, says = CLICKS[do]
    if names != isinstance(raw.get('note'), int) or says != bool(raw.get('text')):
        need = ' and '.join(
            part for part, on in (('a note', names), ('a text', says)) if on
        )
        raise ValueError(f'{chain}: {do} takes {need or "nothing"}')
    return Click(do, raw.get('note'), raw.get('text'))


def _check_notes(chain: Chain) -> None:
    """Every click names a note that is there at that moment."""
    if not chain.notes or not all(chain.notes):
        raise ValueError(f'{chain.id}: notes must be words')
    present = set(range(1, len(chain.notes) + 1))
    added = len(chain.notes)
    for click in chain.clicks:
        if click.do == 'add':
            added += 1
            present.add(added)
        elif click.note not in present:
            raise ValueError(
                f'{chain.id}: {click.do} names note {click.note}, not there'
            )
        elif click.do == 'delete':
            present.discard(click.note)


# --- playing one chain ----------------------------------------------------------


Changes = frozenset[tuple[str, tuple[int, ...], str]]


def changes(interpretation: Interpretation) -> Changes:
    """What the ready edits would write: column, rows, value."""
    return frozenset(
        (g.edit.target, tuple(g.rows), repr(g.value))
        for g in interpretation.ready.values()
    )


@dataclass(frozen=True)
class Compared:
    """A reused answer next to a fresh one, on the sheet it is read on."""

    job: int  # 0: the first reading of every note
    click: str
    note: int
    reused: Changes
    fresh: Changes

    @property
    def same(self) -> bool:
        return self.reused == self.fresh


@dataclass
class Played:
    chain: Chain
    readings: int = 0  # asked by the jobs; one reading is two passes
    compared: list[Compared] = field(default_factory=list)
    careful: int = 0  # read again, the earlier answer made the same edits
    waiting: int = 0  # notes shown with an earlier answer, not compared
    skipped: list[str] = field(default_factory=list)  # clicks the page refuses
    failed: str = ''

    @property
    def ok(self) -> bool:
        return not self.failed and all(c.same for c in self.compared)


@dataclass
class _Note:
    number: int
    text: str


@dataclass
class _Decisions:
    """The two decision files, each key with the note it was made on."""

    confirmed: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)

    def drop(self, number: int) -> None:
        for kept in (self.confirmed, self.rejected):
            for key in [k for k, n in kept.items() if n == number]:
                del kept[key]

    def undecided(self, step: Step) -> list[str]:
        return [
            k
            for k in step.interpretation.ready
            if k not in self.confirmed and k not in self.rejected
        ]


def play(
    chain: Chain, sheet: bytes, resolve: Resolve, fresh: Resolve | None = None
) -> Played:
    """`resolve` answers the jobs; `fresh` the comparison (default: the
    same model). A job the model fails stops the chain."""
    fresh = fresh or resolve
    played = Played(chain)
    notes = [_Note(n, text) for n, text in enumerate(chain.notes, 1)]
    decisions = _Decisions()
    cache: Cache = {}

    def counted(text: str, catalog: Catalog):
        played.readings += 1
        return resolve(text, catalog)

    steps: list[Step] = []
    for job, click in enumerate([None, *chain.clicks]):
        if click is not None:
            refused = _click_on(click, notes, steps, decisions)
            if refused:
                played.skipped.append(f'job {job}, {click.do} {click.note}: {refused}')
                continue
        saved = dict(cache)
        try:
            steps = apply_all(
                sheet,
                [n.text for n in notes],
                counted,
                set(decisions.confirmed),
                cache,
                in_turn=True,
                rejected=frozenset(decisions.rejected),
            )
            label = (
                'read' if click is None else f'{click.do} {click.note or ""}'.strip()
            )
            _compare(played, job, label, notes, steps, sheet, saved, fresh)
        except rt.JobWouldFail as why:
            played.failed = f'job {job}: {why}'
            return played
    return played


def _click_on(  # noqa: PLR0911
    click: Click, notes: list[_Note], steps: list[Step], decisions: _Decisions
) -> str | None:
    """Make the click; why the page refuses it, else None."""
    if click.do == 'add':
        notes.append(_Note(max((n.number for n in notes), default=0) + 1, click.text))
        return None
    at = next(i for i, n in enumerate(notes) if n.number == click.note)
    if click.do == 'reword':
        notes[at].text = click.text
        decisions.drop(click.note)
        return None
    if click.do == 'delete':
        del notes[at]
        decisions.drop(click.note)
        return None
    if click.do == 'undo':
        undone = [k for k, n in decisions.rejected.items() if n == click.note]
        for key in undone:
            del decisions.rejected[key]
        return None if undone else 'no rejection to undo'
    # confirm or reject: the open note's undecided edits, as the server
    # allows (`_open_report`): the first waiting or with something to decide
    open_at = next(
        (i for i, s in enumerate(steps) if s.waiting or decisions.undecided(s)),
        None,
    )
    if open_at is None or open_at != at:
        which = 'none' if open_at is None else f'note {notes[open_at].number}'
        return f'not its turn (open: {which})'
    if steps[at].waiting:
        return 'read again first'
    kept = decisions.confirmed if click.do == 'confirm' else decisions.rejected
    for key in decisions.undecided(steps[at]):
        kept[key] = click.note
    return None


def _compare(  # noqa: PLR0913, PLR0917
    played: Played,
    job: int,
    click: str,
    notes: list[_Note],
    steps: list[Step],
    sheet: bytes,
    saved: Cache,
    fresh: Resolve,
) -> None:
    before = sheet
    for note, step in zip(notes, steps):
        read_on, before = before, step.preview
        if step.waiting:
            played.waiting += 1
            continue
        if step.answer[1] != sha256(read_on):
            # an answer read on another sheet
            asked = interpret_instruction(note.text, read_on, fresh)
            played.compared.append(
                Compared(
                    job,
                    click,
                    note.number,
                    changes(step.interpretation),
                    changes(asked),
                )
            )
        elif step.answer not in saved:
            earlier = _newest(saved, step.answer[0])
            if earlier is not None and changes(
                _interpret(note.text, read_on, earlier)
            ) == changes(step.interpretation):
                played.careful += 1


def _newest(cache: Cache, words: str) -> Answer | None:
    answers = [a for (w, _), a in cache.items() if w == words]
    return max(answers, key=lambda a: a.seq, default=None)


def _interpret(text: str, sheet: bytes, answer: Answer) -> Interpretation:
    """The earlier answer on this sheet, as the job would reuse it."""
    old = answer.old_read or tuple(() for _ in answer.proposal.edits)
    return interpret_instruction(text, sheet, lambda _t, _c: answer.proposal, old)


# --- the model ------------------------------------------------------------------


def asking(call: rt.Ask, chain_id: str) -> Resolve:
    """As run_testcases.asking, each pass kept under the prompt it
    answers: the same note on the same sheet is asked once, whichever
    job or comparison asks it."""

    def ask(text: str, catalog: Catalog):
        opinions = []
        for p in prompts(text, catalog):
            asked = sha256(f'{p.system}\n{p.text}'.encode())[:16]
            name = f'{chain_id}.{asked}.{rt.PASSES[p.second_opinion]}'
            try:
                answer = call(name, p.system, p.text, p.schema)
            except rt.ModelGaveUp as why:
                opinions.append(
                    Unreadable(reason='unreadable', detail=str(why), said=text)
                )
            else:
                opinions.append(read_answer(answer, text, catalog))
        return agree(opinions, catalog)

    return ask


# --- the numbers ----------------------------------------------------------------


def summarize(played: list[Played]) -> dict[str, object]:
    compared = [c for p in played for c in p.compared]
    same = sum(c.same for c in compared)
    return {
        'chains': len(played),
        'passed': sum(p.ok for p in played),
        'failed': sum(bool(p.failed) for p in played),
        'readings': sum(p.readings for p in played),
        'reused': len(compared),
        'same': None if not compared else round(100 * same / len(compared), 1),
        'different': len(compared) - same,
        'careful': sum(p.careful for p in played),
        'waiting': sum(p.waiting for p in played),
    }


def _changed(c: Compared) -> str:
    gone = sorted(c.reused - c.fresh)
    new = sorted(c.fresh - c.reused)
    return f'reused only {gone or "-"}, fresh only {new or "-"}'


def report(played: list[Played]) -> str:
    lines = []
    for p in played:
        lines.append(f'{p.chain.id:<22} {"ok" if p.ok else "FAIL"}')
        lines.append(
            f'{"":<22}   readings {p.readings} · reused {len(p.compared)} · '
            f'read again, same edits {p.careful} · waiting {p.waiting}'
        )
        for c in p.compared:
            if not c.same:
                lines.append(
                    f'{"":<22}   job {c.job} ({c.click}): note {c.note} DIFFERS: '
                    + _changed(c)
                )
        lines.extend(f'{"":<22}   skipped: {s}' for s in p.skipped)
        if p.failed:
            lines.append(f'{"":<22}   the job would fail: {p.failed}')
    s = summarize(played)
    lines.append('')
    lines.append(f'{s["passed"]}/{s["chains"]} chains pass')
    same = '–' if s['same'] is None else f'{s["same"]} %'
    lines.append(
        f'reused answers like a fresh reading: {same} (target 100 %) · '
        f'different {s["different"]}'
    )
    lines.append(
        f'readings {s["readings"]} · saved by reuse {s["reused"]} · '
        f'read again with the same edits {s["careful"]} · waiting {s["waiting"]}'
    )
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--model', help="default: the job's, from nomad.yaml")
    parser.add_argument('--only', help='play this one chain')
    args = parser.parse_args(argv)

    sheet = synthetic()
    chains = load_chains(HERE / 'chains.json')
    chosen = [c for c in chains if args.only in (None, c.id)]
    if not chosen:
        parser.error(f'no chain {args.only!r}; ids: {", ".join(c.id for c in chains)}')
    model = args.model or rt.configured_model()
    print(f'model {model}', file=sys.stderr, flush=True)
    call = rt.cached_model(model, HERE / 'cache' / 'chains')
    played = []
    for number, chain in enumerate(chosen, 1):
        print(f'{number}/{len(chosen)} {chain.id}', file=sys.stderr, flush=True)
        played.append(play(chain, sheet, asking(call, chain.id)))
    print(report(played))
    return 0 if all(p.ok for p in played) else 1


if __name__ == '__main__':
    sys.exit(main())
