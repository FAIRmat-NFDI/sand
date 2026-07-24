"""Resolve NOMAD JSON Schema exports to flat extraction schemas.

NOMAD API exports use JSON Schema $ref / allOf inheritance. This module
resolves that inheritance chain and produces a flat, self-contained schema
suitable for use as an LLM extraction tool definition.
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


def simplify_prop(
    prop: dict,
    defs: dict,
    path: list[str] | None = None,
    max_occurrences: int = 2,
) -> dict | None:
    """Convert a single NOMAD API property to a simplified extraction-schema property.

    Schemas can be cyclic (e.g. ``Solution -> OtherSolution -> Solution``), so
    each class is expanded at most ``max_occurrences`` times per branch;
    beyond that the property is pruned and ``None`` is returned.
    """
    if path is None:
        path = []

    if '$ref' in prop:
        class_name = extract_class_name(prop['$ref'])
        if path.count(class_name) >= max_occurrences:
            return None
        ref_schema = resolve_ref(prop['$ref'], defs)
        sub_props = collect_properties(ref_schema, defs)
        properties = {}
        for k, v in sub_props.items():
            simplified = simplify_prop(
                v, defs, [*path, class_name], max_occurrences
            )
            if simplified is not None:
                properties[k] = simplified
        return {
            'type': 'object',
            'description': prop.get('description', ref_schema.get('description', '')),
            'properties': properties,
        }

    prop_type = prop.get('type', 'string')

    if prop_type == 'array':
        items = prop.get('items', {})
        if '$ref' in items:
            simplified_items = simplify_prop(items, defs, path, max_occurrences)
            if simplified_items is None:
                return None
        else:
            simplified_items = items
        return {
            'type': 'array',
            'description': prop.get('description', ''),
            'items': simplified_items,
        }

    result: dict = {'type': prop_type, 'description': prop.get('description', '')}
    if 'enum' in prop:
        result['enum'] = prop['enum']
    if 'format' in prop:
        result['format'] = prop['format']
    if 'unit' in prop:
        result['unit'] = prop['unit']
    return result


def flatten_schema(api_schema: dict, max_occurrences: int = 2) -> dict:
    """Convert a NOMAD API schema export to a simplified extraction schema."""
    defs = api_schema.get('$defs', {})
    all_props = collect_properties(api_schema, defs)
    simplified = {}
    for k, v in all_props.items():
        prop = simplify_prop(v, defs, max_occurrences=max_occurrences)
        if prop is not None:
            simplified[k] = prop
    return {'type': 'object', 'properties': simplified}
