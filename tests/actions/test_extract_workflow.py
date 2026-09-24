from sand.actions.extract.workflows import _drop_skipped


def test_drop_skipped_keeps_order_and_reports_each_skipped_input():
    texts = ["hi sand, let's record", 'spin coated NiOx on all samples', 'thanks']
    spin = {'step_type': 'Spin Coating', 'variants': []}

    slots, warnings = _drop_skipped(texts, [None, spin, None])

    assert slots == [spin]
    assert [w.split(' (')[0] for w in warnings] == ['input 1', 'input 3']
    assert "hi sand, let's record" in warnings[0]
