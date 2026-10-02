# Terms the live transcript (Deepgram keyterm) is biased towards. Only names
# that are said this way and that a general model gets wrong: plain words
# ("acetone", "annealing") need no help, and short ones ("Cu", "IPA") would
# show up where nobody said them. Spelled as the extraction expects.
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
    # how PbI2 and PbBr2 are often said; "lead" is easily heard as "led"
    'lead iodide',
    'lead bromide',
    # solvents, cleaning
    'DMF',
    'DMSO',
    'chlorobenzene',
    'Hellmanex',
    # processes
    'UV-ozone',
    'anti-solvent',
    'slot-die',
    'ALD',
    'glovebox',
    # tools
    'HySprint',
    'Pero2',
    'Pero5',
    'Ossila',
]
