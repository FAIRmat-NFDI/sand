"""Resolve NOMAD JSON Schema exports to flat extraction schemas.

NOMAD API exports use JSON Schema $ref / allOf inheritance. This module
resolves that inheritance chain and produces a flat.
"""


def extract_class_name(ref_url: str) -> str:
    """Extract the dotted class name from a NOMAD $ref URL."""
    return ref_url.rsplit('/', 1)[-1].split('@', 1)[0]


def resolve_ref(ref_url: str, defs: dict) -> dict:
    """Look up a $ref URL in the schema's $defs."""
    return defs.get(extract_class_name(ref_url), {})


def collect_properties(node: dict, defs: dict, visited: set | None = None) -> dict:
    """Return all properties of a schema node, merging in allOf parent properties first."""
    if visited is None:
        visited = set()

    node_id = node.get('$id', '')
    if node_id in visited:
        return {}
    if node_id:
        visited.add(node_id)

    props: dict = {}

    for parent_ref in node.get('allOf', []):
        if '$ref' in parent_ref:
            parent = resolve_ref(parent_ref['$ref'], defs)
            props.update(collect_properties(parent, defs, visited))

    props.update(node.get('properties', {}))

    return props


def simplify_prop(prop: dict, defs: dict) -> dict:
    """Convert a single NOMAD API property to a simplified extraction-schema property."""
    if '$ref' in prop:
        ref_schema = resolve_ref(prop['$ref'], defs)
        sub_props = collect_properties(ref_schema, defs)
        return {
            'type': 'object',
            'description': prop.get('description', ref_schema.get('description', '')),
            'properties': {k: simplify_prop(v, defs) for k, v in sub_props.items()},
        }

    prop_type = prop.get('type', 'string')

    if prop_type == 'array':
        items = prop.get('items', {})
        if '$ref' in items:
            ref_schema = resolve_ref(items['$ref'], defs)
            sub_props = collect_properties(ref_schema, defs)
            simplified_items = {
                'type': 'object',
                'properties': {
                    k: simplify_prop(v, defs) for k, v in sub_props.items()
                },
            }
        else:
            simplified_items = items
        return {
            'type': 'array',
            'description': prop.get('description', ''),
            'items': simplified_items,
        }

    result: dict = {'type': prop_type, 'description': prop.get('description', '')}
    if 'format' in prop:
        result['format'] = prop['format']
    if 'unit' in prop:
        result['unit'] = prop['unit']
    return result


def flatten_schema(api_schema: dict) -> dict:
    """Convert a NOMAD API schema export to a simplified extraction schema."""
    defs = api_schema.get('$defs', {})
    all_props = collect_properties(api_schema, defs)
    simplified = {k: simplify_prop(v, defs) for k, v in all_props.items()}
    return {'type': 'object', 'properties': simplified}
