"""What survives between runs of the edit job: raw files in the upload,
beside the original sheet.

    sheet_edit/uploaded.xlsx.origin the uploaded sheet, never modified
    sheet_edit/llm_cache.json       the workflow's: what the model answered,
                                    published by every finished job
    sheet_edit/llm_cache.<job>.json one running job's answers (its own)
    sheet_edit/confirmations.json   the API's: which edits the user confirmed
    sheet_edit/edited.xlsx.final    the workflow's: original + confirmed edits
    sheet_edit/changelog.json       the workflow's: what the final sheet changed
    sheet_edit/status.json          the workflow's: progress, previews

One writer per file. The API writes the confirmations, so a confirmation
made in the browser while the job runs is never overwritten by the job.
A running job writes only its own cache file, so two jobs on one upload
cannot lose each other's answers while they run; a job publishes its
answers into the shared cache only at its end, where a lost entry costs
a model call in a later job, never the running one. All are sand's own
working files: nothing parses them, and losing a cache costs model
calls, not data. Not `.xlsx`: NOMAD's batch parser matches
any .xlsx in the upload; the suffixes say which sheet is which.
"""

import datetime
import json
from dataclasses import dataclass, field

from sand.sheet_edit.apply import Answer, Cache, CacheKey
from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import ProposedEdits, Unreadable
from sand.sheet_edit.reads import from_json, to_json
from sand.sheet_edit.workbook import sha256

ORIGINAL_FILE = 'sheet_edit/uploaded.xlsx.origin'
FINAL_FILE = 'sheet_edit/edited.xlsx.final'
CHANGELOG_FILE = 'sheet_edit/changelog.json'
CACHE_FILE = 'sheet_edit/llm_cache.json'
CONFIRMATIONS_FILE = 'sheet_edit/confirmations.json'
# the inputs a Confirm all covered: locked until Reset all
CHECKPOINT_FILE = 'sheet_edit/checkpoint.json'
REJECTIONS_FILE = 'sheet_edit/rejections.json'
STATUS_FILE = 'sheet_edit/status.json'

KEEP_PER_WORDS = 8  # answers kept per words in the shared cache


def job_cache_file(job_id: str) -> str:
    """The running job's own cache file; the id is hashed, Temporal's ids
    hold characters a file name should not."""
    return f'sheet_edit/llm_cache.{sha256(job_id.encode())[:16]}.json'


def cache_key(text: str, state: bytes) -> CacheKey:
    """The key `apply_all` looks proposals up by."""
    return (sha256(text.encode()), sha256(state))


@dataclass
class LlmCache:
    """The model's answers so far, as `apply_all` reads them, plus which
    job got each one."""

    answers: Cache = field(default_factory=dict)
    jobs: dict[CacheKey, str] = field(default_factory=dict)

    def add(self, key: CacheKey, answer: Answer, job: str) -> None:
        self.answers[key] = answer
        self.jobs[key] = job

    def update(self, other: 'LlmCache') -> None:
        """`other`'s entries win."""
        for key, answer in other.answers.items():
            self.add(key, answer, other.jobs[key])

    def of_job(self, job: str, readable_only: bool = False) -> 'LlmCache':
        """The entries one job got; without its unreadable answers when
        they are published for later jobs, which should ask again."""
        cache = LlmCache()
        for key, answer in self.answers.items():
            if self.jobs[key] != job:
                continue
            if readable_only and any(
                isinstance(u, Unreadable) for u in answer.proposal.unresolved
            ):
                continue
            cache.add(key, answer, job)
        return cache

    def trimmed(
        self, keep: set[CacheKey], per_words: int = KEEP_PER_WORDS
    ) -> 'LlmCache':
        """The newest answers of each words, and every one in `keep` (the
        job's own chain). An older answer is what putting things back
        finds again; past a few the sheet has moved on."""
        newest: dict[str, list[CacheKey]] = {}
        # an answer saved before `seq` goes by its place, as in `lookup`
        order = {k: (a.seq, i) for i, (k, a) in enumerate(self.answers.items())}
        for key in sorted(self.answers, key=order.__getitem__, reverse=True):
            newest.setdefault(key[0], []).append(key)
        kept = {k for keys in newest.values() for k in keys[:per_words]} | keep
        cache = LlmCache()
        for key, answer in self.answers.items():
            if key in kept:
                cache.add(key, answer, self.jobs[key])
        return cache


def load_llm_cache(
    data: dict | None, model: str, prompt_version: str, job: str
) -> LlmCache:
    """The stored cache, if it is this model's and this prompt's: another
    model's answers are not answers to this run. An unreadable answer is
    kept only within the job that got it: the state chain needs it, a
    later job should ask again. Anything malformed is dropped: the cache
    is sand's own file and costs only model calls."""
    cache = LlmCache()
    if (
        not isinstance(data, dict)
        or data.get('model') != model
        or data.get('prompt_version') != prompt_version
    ):
        return cache
    entries = data.get('proposals')
    for text, entry in entries.items() if isinstance(entries, dict) else ():
        try:
            proposal = ProposedEdits.model_validate(entry['proposal'])
            got_by = entry['job']
            text_hash, _, state_hash = text.partition(':')
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        unreadable = any(isinstance(u, Unreadable) for u in proposal.unresolved)
        if not isinstance(got_by, str) or (unreadable and got_by != job):
            continue
        seq = entry.get('seq', 0)
        answer = Answer(
            proposal,
            _old_read(entry, proposal),
            from_json(entry['reads']) if 'reads' in entry else None,
            seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
        )
        cache.add((text_hash, state_hash), answer, got_by)
    return cache


def llm_cache_json(cache: LlmCache, model: str, prompt_version: str) -> dict:
    return {
        'model': model,
        'prompt_version': prompt_version,
        'proposals': {
            f'{text}:{state}': _entry_json(answer, cache.jobs[(text, state)])
            for (text, state), answer in cache.answers.items()
        },
    }


def _entry_json(answer: Answer, job: str) -> dict:
    entry = {'proposal': answer.proposal.model_dump(mode='json'), 'job': job}
    if answer.old_read is not None:
        entry['old_read'] = [list(found) for found in answer.old_read]
    if answer.reads is not None:
        entry['reads'] = to_json(answer.reads)
    if answer.seq:
        entry['seq'] = answer.seq
    return entry


def _old_read(
    entry: dict, proposal: ProposedEdits
) -> tuple[tuple[str, ...], ...] | None:
    """One list of messages per edit, or None (not stored, or malformed:
    the answer is then used without an `old` check off its own sheet)."""
    found = entry.get('old_read')
    if (
        isinstance(found, list)
        and len(found) == len(proposal.edits)
        and all(
            isinstance(f, list) and all(isinstance(m, str) for m in f) for f in found
        )
    ):
        return tuple(tuple(f) for f in found)
    return None


def read_confirmations(raw: bytes | None) -> dict:
    """The confirmations file's JSON object; no file is no confirmations.
    Raises ValueError on anything else: a confirmation decides what is
    written, so a damaged file stops the job instead of reading as
    "nothing confirmed"."""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as why:
        raise ValueError(f'the confirmations file is not JSON: {why}') from why
    if not isinstance(data, dict):
        raise ValueError('the confirmations file is not a JSON object')
    return data


def load_confirmations(data: dict) -> dict[str, Confirmation]:
    """By confirmation key, from the file's JSON (`read_confirmations`).
    Raises ValueError on a malformed entry, for the same reason. A record
    made before confirmations named their input is left out: its edit
    waits to be confirmed again."""
    confirmations = {}
    for key, entry in data.items():
        try:
            by, at = entry['by'], entry['at']
            if not isinstance(by, str) or not by or not isinstance(at, str):
                raise TypeError
            when = datetime.datetime.fromisoformat(at)
        except (KeyError, TypeError, ValueError) as why:
            raise ValueError(f'confirmation {key!r} is malformed: {entry!r}') from why
        # absent, not null: an old writer left it out
        if 'input' not in entry:
            continue
        entry_id = entry['input']
        if not isinstance(entry_id, str):
            raise ValueError(f'confirmation {key!r} is malformed: {entry!r}')
        confirmations[key] = Confirmation(
            by=by, at=when, input=entry_id
        )  # naive: ValueError
    return confirmations


def confirmations_json(confirmations: dict[str, Confirmation]) -> dict:
    return {
        key: {
            'by': c.by,
            # to the microsecond: a click's records share it (Undo last)
            'at': c.at.isoformat(),
            'input': c.input,
        }
        for key, c in confirmations.items()
    }


def read_checkpoint(raw: bytes | None) -> set[str]:
    """The entry ids behind the last Confirm all; no file is none.
    Raises ValueError on a damaged file: it decides what may change."""
    if raw is None:
        return set()
    try:
        data = json.loads(raw)
    except ValueError as why:
        raise ValueError(f'the checkpoint file is not JSON: {why}') from why
    inputs = data.get('inputs', []) if isinstance(data, dict) else None
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        raise ValueError('the checkpoint file has no list of inputs')
    return set(inputs)


def read_rejections(raw: bytes | None) -> dict:
    """The rejections file by edit key: {by, at, input, reason, meant,
    proposed}. The job never reads it: a rejected edit is one nobody
    confirmed. Raises ValueError on a damaged file, which is never
    overwritten."""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as why:
        raise ValueError(f'the rejections file is not JSON: {why}') from why
    if not isinstance(data, dict):
        raise ValueError('the rejections file is not a JSON object')
    for key, entry in data.items():
        if not _rejection_ok(entry):
            raise ValueError(f'rejection {key!r} is malformed: {entry!r}')
    return data


_REJECTION_REASONS = (None, 'column', 'samples', 'value', 'other')


def _rejection_ok(entry) -> bool:
    if not isinstance(entry, dict) or set(entry) != {
        'by',
        'at',
        'input',
        'reason',
        'meant',
        'proposed',
    }:
        return False
    try:
        at = datetime.datetime.fromisoformat(entry['at'])
    except (TypeError, ValueError):
        return False
    return (
        isinstance(entry['by'], str)
        and bool(entry['by'])
        and at.tzinfo is not None
        and isinstance(entry['input'], str)
        and entry['reason'] in _REJECTION_REASONS
        and isinstance(entry['meant'], str)
        and (entry['proposed'] is None or isinstance(entry['proposed'], dict))
    )
