"""Shared inline/inspector editors for local utility nodes."""
from PyQt5 import QtCore, QtWidgets
from aetherloom_core.rh_parameters import RhNumberSpinBox, RhEnumComboBox
from . import utility_nodes


def build(panel, node):
    if node['kind'] == 'subgraph':
        from .subgraphs import build as build_group
        build_group(panel,node);return
    if node['kind'] == 'manual_select':
        from .manual_selection import open_selection
        button=QtWidgets.QPushButton('选择结果并继续下游',panel)
        button.clicked.connect(lambda:open_selection(panel,panel.node));panel.form.addWidget(button)
    from .node_form import FieldLabel, NumberDrag
    params = dict(utility_nodes.defaults(node['kind']), **node.get('params', {}))
    fields = list(utility_nodes.SCHEMAS[node['kind']][2])
    if node['kind'] == 'text_template':
        fields += [(port['key'], port['label'], 'line', '', None) for port in utility_nodes.inputs(node)]
    for key, label, kind, default, options in fields:
        caption = FieldLabel(label, panel);caption.setObjectName('canvasFieldLabel')
        panel.form.addWidget(caption)
        value = params.get(key, default)
        if kind == 'text':
            editor = panel._text_editor(str(value), (panel.doc_id, node['id'], key))
            if node['kind'] == 'text_template':
                # Apply explicitly so partially typed braces never remove cables.
                apply = QtWidgets.QPushButton('应用模板 / 更新输入端口', panel)
                def commit(e=editor):
                    try:utility_nodes.template_keys(e.toPlainText())
                    except ValueError as error:panel.message.emit(str(error));return
                    panel.changed.emit('params.template', e.toPlainText())
                apply.clicked.connect(commit);panel.form.addWidget(apply)
                editor.setToolTip('使用 {主题} 等占位符。编辑完成后点击应用；{{ 和 }} 表示字面括号。')
            else:editor.textChanged.connect(lambda k=key,e=editor:panel.changed.emit('params.'+k,e.toPlainText()))
        elif kind in ('int','float'):
            editor = RhNumberSpinBox(integer=kind=='int');editor.configure({'min': options[0], 'max': options[1], 'step': 1 if kind=='int' else .1})
            editor.setValue(value);panel.numeric.append(editor)
            editor.valueChanged.connect(lambda v,k=key:panel.changed.emit('params.'+k,str(v)))
            caption._number_drag = NumberDrag(caption, editor)
            panel.form.addWidget(editor)
        elif kind == 'enum':
            editor = RhEnumComboBox()
            for item, text in options:editor.addItem(text, item)
            editor.setCurrentIndex(max(0,editor.findData(value)))
            editor.currentIndexChanged.connect(lambda _,k=key,e=editor:panel.changed.emit('params.'+k,e.currentData()))
            panel.form.addWidget(editor)
        elif kind == 'bool':
            editor = QtWidgets.QCheckBox(label);caption.hide();editor.setChecked(value)
            editor.toggled.connect(lambda v,k=key:panel.changed.emit('params.'+k,v));panel.form.addWidget(editor)
        else:
            # Display literal backslash-n for a newline delimiter, preserving
            # other text exactly (no broad escape decoding).
            separator = key == 'separator'
            editor = QtWidgets.QLineEdit(str(value).replace('\n', r'\n') if separator else str(value))
            editor.setPlaceholderText(r'\n 表示换行' if separator else '')
            editor.editingFinished.connect(lambda k=key,e=editor,s=separator:panel.changed.emit('params.'+k,e.text().replace(r'\n','\n') if s else e.text()))
            panel.form.addWidget(editor)
        editor.setObjectName('canvasUtility_' + key)
        if node['kind'] == 'image_resize' and key == 'resolution_steps':
            tip = ('1 表示不对齐；常用 8、16、32、64。先计算缩放尺寸，再将宽高分别就近取整到指定倍数，'
                   '最小为一个倍数。可能略微改变比例或超出填写的宽高；图像与遮罩同步处理。')
            caption.setToolTip(tip);editor.setToolTip(tip)
        editor.setEnabled(key not in panel.connected_inputs)
        if any(port['key'] == key for port in utility_nodes.inputs(node)):panel.port_widgets[key] = editor
    if node['kind'] == 'image_compare' and panel.embedded:
        from .image_compare import ImageCompare
        panel.compare_view = ImageCompare(panel)
        panel.compare_view.set_results(node.get('results', []))
        panel.form.addWidget(panel.compare_view)
    hints = {'reroute': '原样传递内容与 List / Batch 分组，用于整理连线。',
             'branch': '条件为真时从上方端口输出，为假时从下方端口输出。未选中的下游分支跳过；List 逐项判断，Batch 保持整组。',
             'manual_select': '运行到此节点后暂停该分支。选择并确认后放行下游；关闭选择窗口不会取消任务，一键终止仍可取消等待。',
             'image_to_mask': 'MASK 是独立灰度遮罩类型，白色表示 1，黑色表示 0。二值 PNG 可直接选择灰度通道；Alpha 默认直接读取，可勾选反相。',
             'mask_to_image': '输出 RGB 灰度 PNG，不写入 Alpha。软遮罩保留灰度；需要二值 PNG 时先连接遮罩二值化。',
             'image_grid': '图像 Batch 拼成一张图；普通 List 逐项运行。缩放后居中放入单元，背景可使用 #RRGGBBAA。',
             'image_composite': '遮罩可不连接；连接时按前景尺寸双线性适配。前景 Alpha、遮罩和透明度共同决定覆盖强度。',
             'video_frames': '最多提取 256 帧，按帧顺序输出图像 List；可同时输出音轨。提取首帧或指定帧时忽略采样间隔。',
             'video_assemble': '图像 Batch 合成一个视频；普通 List 逐项生成。帧尺寸需一致，奇数边补一像素；音轨截断或补静音以匹配视频时长。',
             'json_extract': '使用 JSON Pointer，例如 /scenes/0/prompt。留空提取根值，数组自动输出 List；字段缺失时停止此节点。',
             'media_info': '逐项读取元数据，不解码全部帧。视频帧数按时长 × 帧率估算，可变帧率视频可能有偏差。',
             'note': '仅作为画布说明保存，不参与工作流执行。',
             'text_join': '文本 Batch 合成一段文本；普通 List 逐项通过。需要整体拼接时先使用 List 转 Batch。',
             'image_resize': '分辨率倍数为 1 时按缩放方式输出；大于 1 时宽高分别就近对齐，可能略微改变比例。支持 INT 连线覆盖倍数；输出 RGBA PNG 临时副本，遮罩同步缩放。',
             'image_compare': '运行后按输入顺序两两对比。输出仍为原图像列表，不生成拼接图片。'}
    if node['kind'] in hints:
        hint = QtWidgets.QLabel(hints[node['kind']]);hint.setWordWrap(True);hint.setObjectName('canvasMuted');panel.form.addWidget(hint)
