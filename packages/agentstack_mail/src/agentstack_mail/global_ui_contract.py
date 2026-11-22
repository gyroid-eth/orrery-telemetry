"""Single operator read schema compiler for server and stdlib callers."""
import copy
try:
    from .global_s2b_contract import TOOLS
    from .schema_contract import fixture, validate_schema_definition
except ImportError:
    from global_s2b_contract import TOOLS
    from schema_contract import fixture, validate_schema_definition

FIXTURE = fixture('global-ui.json')


def expand(schema, root=None):
    root = schema if root is None else root
    if isinstance(schema, list):
        return [expand(v, root) for v in schema]
    if not isinstance(schema, dict):
        return schema
    if '$ref' in schema:
        if set(schema) != {'$ref'} or not schema['$ref'].startswith('#/'):
            raise ValueError('local schema reference required')
        target = root
        for key in schema['$ref'][2:].split('/'):
            target = target[key]
        return expand(target, root)
    return {k: expand(v, root) for k, v in schema.items() if k != '$defs'}


def schemas():
    """Derive tool inputs/outputs from their single existing fixture."""
    wire = copy.deepcopy(FIXTURE['operator_read_wire'])
    request, response = wire['request_template'], wire['response_template']
    strip = set(wire['schema_derivation']['strip_input_keys'])
    for tool in ('search_messages', 'summarize_thread', 'fetch_topic'):
        args = copy.deepcopy(TOOLS[tool]['input_schema'])
        args['properties'] = {k:v for k,v in args['properties'].items() if k not in strip}
        args['required'] = [k for k in args['required'] if k not in strip]
        request['$defs']['messages_'+tool]['properties']['arguments'] = args
        response['$defs'][tool] = copy.deepcopy(TOOLS[tool]['output_schema'])
    page = copy.deepcopy(TOOLS['search_messages']['output_schema'])
    response['$defs']['message'] = copy.deepcopy(page['properties']['items']['items'])
    page['properties'].pop('attachment')
    page['required'].remove('attachment')
    for key in ('activity','edge_messages'):
        response['$defs'][key] = copy.deepcopy(page)
    result = expand(request), expand(response)
    for item in result:
        validate_schema_definition(item)
    return result

REQUEST, RESPONSE = schemas()
