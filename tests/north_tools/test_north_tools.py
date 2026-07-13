def test_importing_north_tool():
    # this will raise an exception if pydantic model validation fails for the north tool
    from sand.north_tools.my_north_tool import (
        north_tool_entry_point,
    )

    assert (
        north_tool_entry_point.id_url_safe == 'sand_my_north_tool'
        or north_tool_entry_point.id == 'nomad-north-sand'
    ), 'NORTHtool entry point has incorrect id or id_url_safe'
