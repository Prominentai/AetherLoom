"""Pure input contracts and preflight pruning; never substitute cached outputs."""


def decorate(node, ports):
    from . import model, utility_nodes
    kind = node['kind']
    editable = {field[0] for field in utility_nodes.SCHEMAS.get(kind, ('', '', []))[2]}
    if kind == 'rename':editable |= {'name', 'extension'}
    if kind == 'text_file':editable.add('path')
    if kind in model.MODEL_KINDS:editable |= {'prompt', 'image'}
    optional = {'image_composite': {'mask'}, 'image_mask_composite': {'mask'}, 'image_paste_bounding': {'mask'},
                'video_assemble': {'audio'}}.get(kind, set())
    module = utility_nodes.advanced_nodes.LOCAL_NODES.get(kind)
    if module is not None:
        optional = optional | set(getattr(module, 'OPTIONAL_INPUTS', {}).get(kind, ()))
    group = {port['key'] for port in ports} if kind in model.DYNAMIC_INPUT_KINDS else {'image', 'mask'} if kind == 'mask_preview' else set()
    for port in ports:
        key = port['key']
        port['accepts_local'] = kind == 'app' or key in editable
        port['connection_required'] = not (port['accepts_local'] or key in optional or key in group)
        if key in group:port['connection_group'] = 'at_least_one'
    return ports


def empty(value):
    return value is None or isinstance(value, str) and not value.strip() or isinstance(value, (list, tuple, dict)) and not value


def missing(node, connected):
    from . import model
    if node.get('bypass') or model.unknown_node(node):return []
    ports = model.input_ports(node)
    labels = {port['key']: port['label'] for port in ports}
    issues = [dict(ports=[p['key']], message='请连接：' + p['label']) for p in ports
              if p.get('connection_required') and p['key'] not in connected]
    grouped = [p for p in ports if p.get('connection_group')]
    if grouped and not any(p['key'] in connected for p in grouped):
        issues.append(dict(ports=[p['key'] for p in grouped], message='请至少连接一个输入：' + ' / '.join(p['label'] for p in grouped)))
    params = node.get('params', {})
    if node['kind'] == 'app':
        definitions = {p['fieldKey']: p for p in (node.get('app', {}).get('model_definition') or {}).get('params', []) if 'fieldKey' in p}
        fields = model.app_fields(node)
        from aetherloom_core.rh_multi_inputs import groups, values
        grouped = groups(fields, node.get('app', {}).get('model_definition'))
        members = {index for group in grouped.values() for index in group['indices']}
        for group in grouped.values():
            keys = [model.parameter_key(fields[index]) for index in group['indices']]
            present = any(key in connected or values(params.get(key, fields[index].get('fieldValue'))) for key, index in zip(keys, group['indices']))
            if group['param'].get('required') is True and not present:
                issues.append(dict(ports=keys, message='请导入或连接：' + labels.get(keys[0], keys[0])))
        for index, field in enumerate(fields):
            if index in members:continue
            key = model.parameter_key(field)
            definition = definitions.get(field.get('_model_field') or field.get('fieldName'), {})
            value = params.get(key, field.get('fieldValue'))
            absent = not values(value) if model.field_type(field) in model.MEDIA else empty(value)
            if key not in connected and definition.get('required', field.get('required')) is True and absent:
                issues.append(dict(ports=[key], message='请填写或连接：' + labels.get(key, key)))
    elif node['kind'] in ('vision_model', 'edit_model') and 'image' not in connected and empty(params.get('image')):
        issues.append(dict(ports=['image'], message='请导入图像或连接图像输入'))
    elif node['kind'] in model.MEDIA and empty(params.get('files')):
        issues.append(dict(ports=[], message='请先导入素材或设置输入路径'))
    elif node['kind'] == 'text_file' and 'path' not in connected and empty(params.get('files')):
        issues.append(dict(ports=['path'], message='请导入文本文件或文件夹，或连接路径文本'))
    return issues


def inspect(document):
    from . import model
    connected = {}
    for edge in model.execution_edges(document):connected.setdefault(edge['target'], set()).add(edge['input'])
    return {node['id']: issues for node in document['nodes']
            if (issues := missing(node, connected.get(node['id'], set())))}


def plan(document, target=None):
    from . import model
    requested = model.execution_scope(document, target)
    issues = {key: value for key, value in inspect(document).items() if key in requested}
    outgoing = {}
    for edge in model.execution_edges(document):outgoing.setdefault(edge['source'], []).append(edge['target'])
    blocked, pending = set(), list(issues)
    while pending:
        key = pending.pop()
        if key in blocked:continue
        blocked.add(key);pending.extend(outgoing.get(key, []))
    # Trace back only from valid targets; an invalid branch must not awaken its
    # otherwise-unused upstream work, even when that upstream has valid inputs.
    if target is None:
        targets = model.output_targets(document)
    else:
        values = list(target) if isinstance(target, (list, tuple)) else [target]
        groups = {n['id']: n.get('members', []) for n in document['nodes'] if n['kind'] == 'subgraph'}
        targets = [child for value in values for child in groups.get(value, [value])]
    targets = [key for key in targets if key not in blocked]
    scope = model.ancestors(document, targets) & requested if targets else set()
    return dict(scope=scope, requested=requested, issues=issues, skipped=requested - scope)


def display_issue(node):
    """Only an explicit run or execution may promote an input hint to an error."""
    if node.get('bypass'):return ''
    if node.get('_input_issue_confirmed') and node.get('_input_issue'):
        return node['_input_issue']
    if node.get('stale') or node.get('_ui_stale'):return ''
    return node.get('_runtime_input_issue') or ''


def display_missing_ports(node):
    if not display_issue(node):return []
    key = '_input_missing_ports' if node.get('_input_issue_confirmed') and node.get('_input_issue') else '_runtime_missing_ports'
    return node.get(key) or []


def refresh(page, *, validated=None):
    """View-only diagnostics; editing a running graph never changes its plan."""
    issues = inspect(page.document)
    for node in page.document['nodes']:
        details = issues.get(node['id'], [])
        message = '；'.join(issue['message'] for issue in details)
        ports = [key for issue in details for key in issue['ports']]
        if (message, ports) != (node.get('_input_issue', ''), node.get('_input_missing_ports', [])):
            node.pop('_input_issue_confirmed', None)
        node['_input_issue'] = message
        node['_input_missing_ports'] = ports
        if validated is not None and node['id'] in validated:
            node['_input_issue_confirmed'] = bool(details)
        item = page.scene.nodes.get(node['id'])
        if item is not None:
            item.refresh_bypass()
            for port in item.ports.values():port.refresh_connection(port.connected)
            item.update()
    return issues
