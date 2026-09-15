"""Presentation of unavailable dependencies; never persisted as task status."""
from . import model


def notice(node, apps):
    if model.unknown_node(node):
        return dict(label='不支持的节点', severity='danger',
                    detail=f"{model.node_title(node)}：当前客户端不支持类型 {node['kind']}。参数和连线已保留，请更新客户端或替换节点。")
    if node.get('kind') != 'app':return None
    from aetherloom_core.rh_model_apps import LABELS
    app = node.get('app') or {}
    label = LABELS.get(app.get('backend', 'rh_app'), '应用')
    try:
        model.app_reference(app)
    except ValueError as error:
        return dict(label=label+'引用无效', severity='danger', detail=str(error))
    if str(app.get('webapp_id', '')) not in apps:
        return dict(label='缺少'+label, severity='warning',
                    detail=f'此{label}尚未添加到本机。点击画布顶部“一键补齐”，或打开节点设置单独补齐；现有参数和连线保持不变。')
    return None


def refresh(page):
    for item in page.scene.nodes.values():
        issue = notice(item.node, page.apps)
        if item.node.get('kind') == 'subgraph':
            members = set(item.node.get('members', []))
            count = sum(bool(notice(n, page.apps)) for n in page.document['nodes'] if n['id'] in members)
            if count:issue = dict(label=f'{count} 个成员待补齐', severity='warning', detail='展开组合查看缺失依赖或不支持的节点。')
        item.dependency_issue = issue
        item.refresh_bypass()
        item.update()
