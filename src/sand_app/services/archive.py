def to_filename(name: str) -> str:
    """Turn a process name like 'Substrate Cleaning' into 'Substrate_Cleaning'."""
    return name.replace(' ', '_')


def build_archive(process: dict) -> dict:
    """Wrap an extracted process dict in the NOMAD archive format."""
    return {
        'data': {
            'm_def': 'nomad.datamodel.metainfo.eln.ELNProcess',
            **process,
        }
    }
