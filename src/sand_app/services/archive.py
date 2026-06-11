from sand_app.services.perovskite_export import cell_to_archive


def to_filename(name: str) -> str:
    """Turn a name like 'Substrate Cleaning' into 'Substrate_Cleaning'."""
    return name.replace(' ', '_')


def build_archive(cell: dict) -> dict:
    """Wrap an extracted perovskite solar cell in the NOMAD archive format."""
    return cell_to_archive(cell)
