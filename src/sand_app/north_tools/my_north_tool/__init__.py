from nomad.config.models.north import NORTHTool
from nomad.config.models.plugins import NorthToolEntryPoint

my_north_tool = NORTHTool(
    short_description='Jupyter Notebook server in NOMAD NORTH for NOMAD plugin sand-app.',
    image='ghcr.io/fairmat-nfdi/sand-app:main',
    description='Jupyter Notebook server in NOMAD NORTH for NOMAD plugin sand-app.',
    external_mounts=[],
    file_extensions=['ipynb'],
    icon='logo/jupyter.svg',
    image_pull_policy='Always',
    default_url='/lab',
    maintainer=[{'email': 'yaru.wang@physik.hu-berlin.de', 'name': 'Yaru Wang'}],
    mount_path='/home/jovyan',
    path_prefix='lab/tree',
    privileged=False,
    with_path=True,
    display_name='my_north_tool',
)

north_tool_entry_point = NorthToolEntryPoint(
    id_url_safe='sand_app_my_north_tool', north_tool=my_north_tool
)
