"""Non-secret helper choices for a statically admitted native selection.

No runtime import, process, credential lookup or model discovery. The catalog
and POST validator use the same choices; generator support is declared locally.
"""
import ast
from pathlib import Path

SAME = {'id': 'same', 'label': 'Same as main'}


def model_ids(models):
    result = []
    for item in models if isinstance(models, list) else []:
        value = item.get('id') if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip() and value not in result:
            result.append(value)
    return result


def configured_models(manifest, backend_id):
    """Explicit same-backend IDs only; never synthesize vendor model lists."""
    spec = manifest.get('backends', {}).get(backend_id, {})
    models = spec.get('models', [spec.get('model')])
    if not isinstance(models, list) or not all(isinstance(item, str) and item.strip() for item in models):
        return []
    return model_ids(models)


def generator_supports_helpers(product_root, harness):
    if harness not in ('dsh', 'hermes'):
        return False
    source = Path(product_root) / 'integrations' / harness / 'thin_config.py'
    try:
        return any(isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'HELPER_MODEL_CONTRACT'
            for target in node.targets) and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int and node.value.value == 1
            for node in ast.parse(source.read_text()).body)
    except (OSError, SyntaxError, UnicodeError):
        return False


def helper_models(selection, *, manifest, backend_id, public_models, product_root, harness):
    """Selection is the successful shared selection() result, or None.

    Alternate choices must be both configured on the selected backend and in
    its public model catalog. Only IDs/labels cross the public boundary.
    """
    choices = [dict(SAME)]
    if not selection or not generator_supports_helpers(product_root, harness):
        return choices
    configured = configured_models(manifest, backend_id)
    for model in model_ids(public_models):
        if model in configured and model != 'same':
            choices.append({'id': model, 'label': model})
    return choices


def validate_helper_model(value, choices):
    """Normalize omitted helper to same; reject undeclared or foreign IDs."""
    if value is None:
        return 'same'
    if not isinstance(value, str) or value not in model_ids(choices):
        raise ValueError('invalid_helper_model')
    return value


def model_rows(manifest, backend_id, *, selections, statuses, product_root):
    """Project each formal model's independently checked harness capability.

    Internal maps use (model, harness) keys. selections holds shared selection()
    results; statuses holds the availability/I5 result for that exact pair.
    Never reuse the primary model's capability for another configured model.
    """
    models = configured_models(manifest, backend_id)
    rows = []
    for model in models:
        harnesses = []
        for harness in ('dsh', 'hermes'):
            pair = (model, harness)
            status = statuses.get(pair, {})
            selected = selections.get(pair)
            available = bool(selected) and status.get('available') is True
            helpers = [candidate for candidate in models
                       if selections.get((candidate, harness))
                       and statuses.get((candidate, harness), {}).get('available') is True]
            info = {
                'id': harness, 'available': available, 'default': False,
                'reason': None if available else status.get('reason') or 'runtime_not_prepared',
                'efforts': status.get('efforts') if available else None,
                'default_effort': status.get('default_effort') if available else None,
                'effort_source': status.get('effort_source', 'unknown') if available else 'unknown',
                'helper_models': helper_models(selected if available else None,
                    manifest=manifest, backend_id=backend_id, public_models=helpers,
                    product_root=product_root, harness=harness),
            }
            harnesses.append(info)
        first = next((item for item in harnesses if item['available']), None)
        if first:
            first['default'] = True
        rows.append({'id': model, 'label': model, 'harnesses': harnesses})
    return rows
