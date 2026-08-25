from sand.apis import sand_api


def test_sand_api_entry_point():
    assert sand_api.prefix == 'sand'
    assert sand_api.name == 'SAND API'
