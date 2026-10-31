"""What survives between runs of the edit job: raw files in the upload,
beside the original sheet.

    sheet_edit/original.xlsx.orig   the uploaded sheet, never modified
    sheet_edit/llm_cache.json       the workflow's: what the model answered,
                                    published by every finished job
    sheet_edit/llm_cache.<job>.json one running job's answers (its own)
    sheet_edit/confirmations.json   the API's: which edits the user confirmed
    sheet_edit/final.xlsx.orig      the workflow's: original + confirmed edits
    sheet_edit/changelog.json       the workflow's: what the final sheet changed
    sheet_edit/status.json          the workflow's: progress, previews

One writer per file. The API writes the confirmations, so a confirmation
made in the browser while the job runs is never overwritten by the job.
A running job writes only its own cache file, so two jobs on one upload
cannot lose each other's answers while they run; a job publishes its
answers into the shared cache only at its end, where a lost entry costs
a model call in a later job, never the running one. All are sand's own
working files: nothing parses them, and losing a cache costs model
calls, not data. `.orig`, not `.xlsx`: NOMAD's hysprint batch parser
matches any .xlsx in the upload.
"""

import datetime
import json
from dataclasses import dataclass, field

from sand.sheet_edit.apply import Cache
from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import ProposedEdits, Unreadable
from sand.sheet_edit.workbook import sha256

ORIGINAL_FILE = 'sheet_edit/original.xlsx.orig'
FINAL_FILE = 'sheet_edit/final.xlsx.orig'
CHANGELOG_FILE = 'sheet_edit/changelog.json'
CACHE_FILE = 'sheet_edit/llm_cache.json'
CONFIRMATIONS_FILE = 'sheet_edit/confirmations.json'
STATUS_FILE = 'sheet_edit/status.json'

CacheKey = tuple[str, str]


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

    proposals: Cache = field(default_factory=dict)
    jobs: dict[CacheKey, str] = field(default_factory=dict)

    def add(self, key: CacheKey, proposal: ProposedEdits, job: str) -> None:
        self.proposals[key] = proposal
        self.jobs[key] = job

    def update(self, other: 'LlmCache') -> None:
        """`other`'s entries win."""
        for key, proposal in other.proposals.items():
            self.add(key, proposal, other.jobs[key])

    def of_job(self, job: str, readable_only: bool = False) -> 'LlmCache':
        """The entries one job got; without its unreadable answers when
        they are published for later jobs, which should ask again."""
        cache = LlmCache()
        for key, proposal in self.proposals.items():
            if self.jobs[key] != job:
                continue
            if readable_only and any(
                isinstance(u, Unreadable) for u in proposal.unresolved
            ):
                continue
            cache.add(key, proposal, job)
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
        cache.add((text_hash, state_hash), proposal, got_by)
    return cache


def llm_cache_json(cache: LlmCache, model: str, prompt_version: str) -> dict:
    return {
        'model': model,
        'prompt_version': prompt_version,
        'proposals': {
            f'{text}:{state}': {
                'proposal': proposal.model_dump(mode='json'),
                'job': cache.jobs[(text, state)],
            }
            for (text, state), proposal in cache.proposals.items()
        },
    }


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
    Raises ValueError on a malformed entry, for the same reason."""
    confirmations = {}
    for key, entry in data.items():
        try:
            by, at = entry['by'], entry['at']
            if not isinstance(by, str) or not by or not isinstance(at, str):
                raise TypeError
            when = datetime.datetime.fromisoformat(at)
        except (KeyError, TypeError, ValueError) as why:
            raise ValueError(f'confirmation {key!r} is malformed: {entry!r}') from why
        confirmations[key] = Confirmation(by=by, at=when)  # ValueError if naive
    return confirmations


def confirmations_json(confirmations: dict[str, Confirmation]) -> dict:
    return {
        key: {'by': c.by, 'at': c.at.isoformat(timespec='seconds')}
        for key, c in confirmations.items()
    }
