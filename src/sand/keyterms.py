from sand.sheet_edit.catalog import Catalog
from sand.sheet_edit.workbook import EXPERIMENT_INFO

# The lab's fixed terms for the live transcript; each sheet adds its own
# (sheet_keyterms). Only names
# that are said this way and that a general model gets wrong: plain words
# ("acetone", "annealing") need no help, and short ones ("Cu", "IPA") would
# show up where nobody said them. Spelled as the sheets write them.
KEYTERMS = [
    # materials, precursors
    'perovskite',
    'NiOx',
    'Me-4PACz',
    'PEAI',
    'BCP',
    'PCBM',
    'C60',
    'ITO',
    'FTO',
    'CsI',
    'FAI',
    'MABr',
    'MACl',
    'PbI2',
    'PbBr2',
    # the spoken names: long chemical words a general model rarely knows,
    # and "lead" is easily heard as "led"
    'lead iodide',
    'lead bromide',
    'caesium iodide',
    'formamidinium',
    'methylammonium',
    'phenethylammonium',
    'bathocuproine',
    # layers, said as letters
    'HTL',
    'ETL',
    # solvents, cleaning
    'DMF',
    'DMSO',
    'chlorobenzene',
    'CBZ',
    'EtOH',
    'DI water',
    'Hellmanex',
    # processes
    'UV-ozone',
    'anti-solvent',
    'slot-die',
    'ALD',
    'glovebox',
    # tools
    'Pero2',
    'Pero5',
    'Ossila',
]


# 'Ag', 'N2': too short, they would be heard where nobody said them
_MIN_LENGTH = 3


def sheet_keyterms(catalog: Catalog) -> list[str]:
    """The sheet's own words, most useful first: materials, solvents and
    solutes, then variation labels, then step types.

    From the sheet as uploaded: words an edit brings in later are left
    out on purpose, the list is fixed per sheet.
    """
    names, variations = [], []
    for col in catalog.columns:
        values = [g.value for g in col.groups if isinstance(g.value, str)]
        header = col.name.lower()
        if header == 'variation' and col.column.section.step_type == EXPERIMENT_INFO:
            # '15min-115°C' is a value, said in words and numbers; a label
            # like 'reference' is a term
            variations += [v for v in values if not any(ch.isdigit() for ch in v)]
        elif _names_a_substance(header):
            # 'MAI+MABr': each material is said on its own
            names += [part for value in values for part in value.split('+')]
    steps = [s.step_type for s in catalog.structure.sections]
    steps = [s for s in steps if s != EXPERIMENT_INFO]
    return _distinct([*names, *variations, *steps])


def _names_a_substance(header: str) -> bool:
    """'Material name 2', 'Substrate material', 'Solvent 1 name', 'Solute 1
    type'; not 'Material state' or 'Solvent 1 volume' (not text anyway)."""
    if header.startswith(('solvent', 'solute')):
        return True
    return 'material name' in header or header == 'substrate material'


def _distinct(terms: list[str]) -> list[str]:
    seen, kept = set(), []
    for raw in terms:
        term = ' '.join(raw.split())
        key = term.lower()
        if len(term) < _MIN_LENGTH or key in seen:
            continue
        seen.add(key)
        kept.append(term)
    return kept
