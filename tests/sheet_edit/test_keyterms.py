from synthetic_sheet import edited, letter

from sand.keyterms import sheet_keyterms
from sand.sheet_edit import catalog

VARIATION = letter('Experiment Info', 'Variation')


def test_the_sheet_names_its_substances_then_its_steps(synthetic_xlsx):
    assert sheet_keyterms(catalog.build(synthetic_xlsx)) == [
        'Acetone',
        'IPA',
        'Me-4PACz',
        'Ethanol',
        'SAM',
        'Ethanol wash',
        'PbI2',
        'MAI',
        'MABr',
        'Cleaning UV-Ozone',
        'Spin Coating',
        'Seq-Evaporation',
        'Close Space Sublimation',
        'Annealing',
        'Evaporation',
    ]


def test_a_mixture_is_split_and_a_short_name_left_out(synthetic_xlsx):
    terms = sheet_keyterms(catalog.build(synthetic_xlsx))

    assert 'MAI+MABr' not in terms
    assert 'Ag' not in terms  # 8: Evaporation's material, too short


def test_a_variation_label_counts_a_variation_value_does_not(synthetic_xlsx):
    def label_two_rows(ws):
        ws[f'{VARIATION}3'] = 'reference'
        ws[f'{VARIATION}4'] = '  Reference '

    terms = sheet_keyterms(catalog.build(edited(synthetic_xlsx, label_two_rows)))

    assert terms.count('reference') == 1
    assert terms.index('reference') == terms.index('MABr') + 1
    assert not [t for t in terms if 'min' in t]  # '10min', '20min-hot'


def test_the_real_sheet_gives_its_chemicals(real_kit_xlsx):
    terms = sheet_keyterms(catalog.build(real_kit_xlsx))

    assert {'PbI2', 'MAI', 'MABr', 'C60', 'BCP'} <= set(terms)
    assert not [t for t in terms if t[0].isdigit()]
