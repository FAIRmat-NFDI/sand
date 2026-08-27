import json
import re
from datetime import date

# Bookkeeping keys that are not part of a step's identity.
_NON_CONTENT = ('samples', 'position_in_experimental_plan')


def _content_key(step: dict) -> str:
    """A step's dedup identity: its content minus bookkeeping, as canonical JSON."""
    return json.dumps(
        {k: v for k, v in step.items() if k not in _NON_CONTENT},
        sort_keys=True,
        ensure_ascii=False,
    )


def _collapse_all(ids, all_ids):
    """Full coverage -> "all"; otherwise the sample list unchanged."""
    return 'all' if set(ids) == set(all_ids) else ids


def canonicalize(archive: dict) -> dict:
    """Normalize an assembled archive: within each plan slot, identical steps
    merge (samples unioned), full coverage collapses to "all", steps are
    ordered by position. Idempotent. Every step must carry
    `position_in_experimental_plan` or this raises."""
    samples = archive.get('samples', [])
    order = {
        (str(s.get('lab_id') or '').strip() or f'row{i + 1}'): i
        for i, s in enumerate(samples)
    }
    all_ids = set(order)

    slots: dict[tuple, dict] = {}
    for step in archive.get('steps', []):
        pos = step.get('position_in_experimental_plan')
        if pos is None:
            raise ValueError(
                f'canonicalize: step {step.get("step_type")!r} has no '
                'position_in_experimental_plan'
            )
        entry = slots.setdefault(
            (pos, _content_key(step)), {'pos': pos, 'step': step, 'ids': set()}
        )
        s = step.get('samples')
        entry['ids'].update(all_ids if s == 'all' else (s or []))

    steps = []
    for e in sorted(slots.values(), key=lambda e: e['pos']):
        step = dict(e['step'])
        step['samples'] = _collapse_all(
            sorted(e['ids'], key=lambda x: order.get(x, len(order))), all_ids
        )
        steps.append(step)
    return {'samples': samples, 'steps': steps}


def _sample_names(first: str, n: int) -> list[str]:
    """first "1" -> 1..n, first "x_1" -> x_1..x_n. A first sample without a
    trailing number cannot be auto-continued -> ValueError."""
    m = re.match(r'^(.*?)(\d+)$', str(first))
    if not m:
        raise ValueError(f'first sample {first!r} has no trailing number to count from')
    prefix, start = m.group(1), int(m.group(2))
    return [f'{prefix}{start + i}' for i in range(n)]


def _nomad_id(project: str, batch, subbatch, sample: str) -> str:
    return f'{project}_{batch}_{subbatch}_C-{sample}'


def build_samples(info: dict) -> list[dict]:
    """The Experiment-Info rows from the form: generated sample names + lab_ids.

    `date` is stored as an ISO string - the format NOMAD's hysprint batch
    parser accepts from the sheet's Date column (a bare spreadsheet serial
    would be silently dropped there). Accepts a datetime.date, an ISO
    string (validated), or None (today)."""
    iso_date = _iso_date(info.get('date'))
    return [
        {
            'date': iso_date,
            'project_name': info['project_name'],
            'batch': info['batch'],
            'subbatch': info['subbatch'],
            'sample': name,
            'lab_id': _nomad_id(
                info['project_name'], info['batch'], info['subbatch'], name
            ),
        }
        for name in _sample_names(info['first_sample'], info['n_samples'])
    ]


def append_step(archive: dict, slot: dict, pos: int) -> dict:
    """Add one extracted step (its variants) at plan position `pos`. The
    extractor names samples by LABEL ("1"); the archive keys steps by lab_id.
    A label the form did not declare raises."""
    by_label = {s['sample']: s['lab_id'] for s in archive['samples']}
    for variant in slot['variants']:
        samples = variant['samples']
        if samples == 'all':
            who = 'all'
        else:
            who = []
            for label in samples:
                if label not in by_label:
                    raise ValueError(
                        f'step names sample {label!r}, not among {sorted(by_label)}'
                    )
                who.append(by_label[label])
        fields = {k: v for k, v in variant.items() if k != 'samples'}
        archive['steps'].append(
            {
                'step_type': slot['step_type'],
                'position_in_experimental_plan': pos,
                'samples': who,
                **fields,
            }
        )
    return archive


def compose_experiment(info: dict, slots: list[dict]) -> dict:
    """Experiment-info form + ordered step slots -> {samples, steps}."""
    archive = {'samples': build_samples(info), 'steps': []}
    for pos, slot in enumerate(slots, start=1):
        append_step(archive, slot, pos)
    return archive
