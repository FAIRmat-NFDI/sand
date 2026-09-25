from sand.apis import sand_api


def test_sand_api_entry_point():
    assert sand_api.id_url_safe == 'sand'
    assert sand_api.name == 'SAND'
