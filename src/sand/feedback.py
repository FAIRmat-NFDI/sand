"""User reports of what's not working ("Something's wrong").

A report is a raw JSON file in the sheet's own upload, under
`sand_feedback/`: not an input (the sheet editor never reads it), and
removed in one go by deleting the folder. Delete it before the upload is
published: published raw files are public.

A report copies what the user saw (inputs, edit status, confirmations)
because the sheet keeps changing after it.
"""

import dataclasses
import datetime
from importlib.metadata import PackageNotFoundError, version

FEEDBACK_DIR = 'sand_feedback'


def sand_version() -> str:
    try:
        return version('nomad-sand')
    except PackageNotFoundError:
        return 'unknown'


def report_file(at: datetime.datetime) -> str:
    # microseconds: two reports in one second must not overwrite each other
    return f'{FEEDBACK_DIR}/report_{at.strftime("%Y%m%dT%H%M%S%fZ")}.json'


def report(  # noqa: PLR0913
    *,
    at: datetime.datetime,
    by: str,
    what_wrong: str,
    expected: str,
    page: str,
    client: dict,
    sheet: dict,
    inputs: list | None,
    status: dict | None,
    confirmations: dict | None,
    context_error: str | None,
    edit: dict | None = None,
) -> dict:
    """`context_error`: why the snapshot is missing. The report is kept
    anyway: a broken sheet is exactly when users report."""
    return {
        # 'edit': the user marked one proposed edit wrong; 'page': the button
        'kind': 'page' if edit is None else 'edit',
        'edit': edit,
        'created_at': at.isoformat(),
        'by': by,
        'sand_version': sand_version(),
        'page': page,
        'what_wrong': what_wrong,
        'expected': expected,
        'sheet': sheet,
        'context': {
            'inputs': [dataclasses.asdict(item) for item in inputs or []],
            'edit_status': status,
            'confirmations': confirmations,
            'error': context_error,
        },
        'client': client,
        'status': 'new',
    }


def proposed_edit(status: dict | None, key: str) -> dict | None:
    """The edit with this key as the job reported it, with its input's
    words; None when the status no longer has it (a newer job ran)."""
    for report in (status or {}).get('inputs') or []:
        if not isinstance(report, dict):
            continue
        for edit in report.get('ready') or []:
            if isinstance(edit, dict) and edit.get('key') == key:
                return {
                    'input': report.get('input'),
                    'entry_id': report.get('entry_id'),
                    'text': report.get('text'),
                    **edit,
                }
    return None
