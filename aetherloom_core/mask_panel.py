"""Mask editor tool rail, transforms and per-layer controls."""
from PIL import Image
import os
from PyQt5 import QtCore,QtGui,QtWidgets
from .mask_canvas import AdvancedMaskCanvas
from .mask_assets import matches


def build(dialog,path,mask):
    layout=QtWidgets.QVBoxLayout(dialog);layout.setContentsMargins(12,12,12,10);layout.setSpacing(8)
    canvas=dialog.canvas=AdvancedMaskCanvas(path,dialog);canvas.setMinimumHeight(120)
    if matches(mask,path):canvas.restore_config(mask)
    tabs=dialog.edit_tabs=QtWidgets.QTabBar(dialog);tabs.setObjectName('imageEditTabs')
    tabs.addTab('遮罩绘制');tabs.addTab('直接绘制');tabs.setExpanding(True);layout.addWidget(tabs)
    toolbar=QtWidgets.QHBoxLayout()
    dialog.undo_button=QtWidgets.QPushButton('撤销');dialog.redo_button=QtWidgets.QPushButton('重做')
    for button,callback in [(dialog.undo_button,canvas.undo),(dialog.redo_button,canvas.redo)]:button.clicked.connect(callback);toolbar.addWidget(button)
    transforms=[('左转','左转 90°',Image.Transpose.ROTATE_90),('右转','右转 90°',Image.Transpose.ROTATE_270),
                ('水平','水平镜像',Image.Transpose.FLIP_LEFT_RIGHT),('垂直','垂直镜像',Image.Transpose.FLIP_TOP_BOTTOM)]
    for label,hint,operation in transforms:
        button=QtWidgets.QPushButton(label);button.setToolTip(hint+'（所有图层）');button.setFixedWidth(52)
        button.clicked.connect(lambda unused=False,op=operation:canvas.transform_all(op));toolbar.addWidget(button)
    toolbar.addStretch()
    importer=QtWidgets.QPushButton('导入');importer.setToolTip('导入遮罩图像');importer.setObjectName('maskImportButton');importer.clicked.connect(lambda:dialog.import_paint() if tabs.currentIndex()==1 else dialog.import_mask());toolbar.addWidget(importer)
    mask_controls=[]
    for label,callback in [('反选',lambda:canvas.transform_mask(True)),('清空',lambda:canvas.clear_paint() if tabs.currentIndex()==1 else canvas.transform_mask(False))]:
        button=QtWidgets.QPushButton(label);button.clicked.connect(callback);toolbar.addWidget(button)
        if label=='反选':mask_controls.append(button)
    layout.addLayout(toolbar)
    path_rows={}
    for label,value,attribute in [('图像路径',str(path),'source_path_display'),
        ('遮罩路径',str((mask or {}).get('path') or '') if os.path.isfile(str((mask or {}).get('path') or '')) else str((mask or {}).get('import_path') or ''),'mask_path_display'),
        ('绘画路径',str((mask or {}).get('paint_path') or '') if os.path.isfile(str((mask or {}).get('paint_path') or '')) else str((mask or {}).get('paint_import_path') or ''),'paint_path_display')]:
        container=QtWidgets.QWidget();row=QtWidgets.QHBoxLayout(container);row.setContentsMargins(0,0,0,0);row.addWidget(QtWidgets.QLabel(label))
        display=QtWidgets.QLineEdit(value);display.setReadOnly(True);display.setPlaceholderText('尚未保存；运行时保存到输入目录 / '+('paintings' if attribute=='paint_path_display' else 'masks'))
        display.setToolTip(value);setattr(dialog,attribute,display);row.addWidget(display,1);layout.addWidget(container);path_rows[attribute]=container
    body=QtWidgets.QHBoxLayout();body.setSpacing(8)
    rail_content=QtWidgets.QWidget();rail=QtWidgets.QVBoxLayout(rail_content);rail.setContentsMargins(0,0,0,0);rail.setSpacing(6)
    group=QtWidgets.QButtonGroup(dialog);group.setExclusive(True)
    buttons={}
    tools=[('mask','遮罩笔','B'),('paint','绘画笔','P'),('erase','橡皮擦','E'),('fill','油漆桶','G'),('color','选色区','C')]
    def choose(tool):
        if tool in ('mask','fill','color'):tabs.setCurrentIndex(0)
        elif tool=='paint':tabs.setCurrentIndex(1)
        canvas.finish_stroke();canvas.tool=tool
        if tool in ('mask','paint'):canvas.active_layer=tool
        canvas.update();dialog.refresh()
    for tool,label,key in tools:
        button=QtWidgets.QPushButton(label);button.setObjectName('maskTool_'+tool);button.setCheckable(True);button.setFixedSize(78,32)
        button.setToolTip(label+' · '+key);button.clicked.connect(lambda unused=False,t=tool:choose(t));group.addButton(button);rail.addWidget(button)
        buttons[tool]=button
        shortcut=QtWidgets.QShortcut(QtGui.QKeySequence(key),dialog);shortcut.activated.connect(button.click)
    buttons['mask'].setChecked(True);dialog.paint_button=buttons['mask'];dialog.erase_button=buttons['erase'];dialog.tool_buttons=buttons
    rail.addStretch()
    for label,callback in [('适应',canvas.fit),('100%',canvas.actual_size)]:
        button=QtWidgets.QPushButton(label);button.setFixedSize(78,32);button.clicked.connect(callback);rail.addWidget(button)
    rail_scroll=QtWidgets.QScrollArea();rail_scroll.setFrameShape(QtWidgets.QFrame.NoFrame);rail_scroll.setWidgetResizable(True)
    rail_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff);rail_scroll.setFixedWidth(94);rail_scroll.setWidget(rail_content)
    body.addWidget(rail_scroll);body.addWidget(canvas,1)
    sidebar=QtWidgets.QScrollArea();sidebar.setFrameShape(QtWidgets.QFrame.NoFrame);sidebar.setWidgetResizable(True)
    sidebar.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff);sidebar.setFixedWidth(204)
    content=QtWidgets.QWidget();settings=QtWidgets.QVBoxLayout(content);settings.setContentsMargins(6,0,6,0);settings.setSpacing(7)
    sidebar.setWidget(content);body.addWidget(sidebar);layout.addLayout(body,1)
    from .rh_parameters import RhNumberSpinBox,RhEnumComboBox
    settings.addWidget(QtWidgets.QLabel('笔刷设置'))
    shape=RhEnumComboBox();shape.addItem('圆形','round');shape.addItem('方形','square')
    shape.currentIndexChanged.connect(lambda:(setattr(canvas,'shape',shape.currentData()),canvas.update()));settings.addWidget(shape)
    dialog.size=RhNumberSpinBox(integer=True);dialog.size.configure({'min':1,'max':1024});dialog.size.setValue(40)
    dialog.size.valueChanged.connect(lambda value:(setattr(canvas,'diameter',int(value)),canvas.update()))
    settings.addWidget(QtWidgets.QLabel('大小 / 像素'));settings.addWidget(dialog.size)
    dialog.brush_sliders={}
    for key,label,value in [('hardness','硬度',100),('brush_opacity','笔触不透明度',100),('spacing','间距',10)]:
        settings.addWidget(QtWidgets.QLabel(label));slider=QtWidgets.QSlider(QtCore.Qt.Horizontal);slider.setRange(1 if key=='spacing' else 0,100);slider.setValue(value)
        slider.valueChanged.connect(lambda value,k=key:setattr(canvas,k,value/100));settings.addWidget(slider);dialog.brush_sliders[key]=slider
    color_button=QtWidgets.QPushButton('笔刷 / 遮罩颜色…')
    def set_color():
        key='color' if canvas.tool=='paint' or canvas.active_layer=='paint' else 'mask_color'
        color=QtWidgets.QColorDialog.getColor(getattr(canvas,key),dialog,'绘画颜色' if key=='color' else '遮罩颜色',QtWidgets.QColorDialog.ShowAlphaChannel if key=='color' else QtWidgets.QColorDialog.ColorDialogOptions())
        if color.isValid():setattr(canvas,key,color);canvas._overlay=None;canvas.update();color_button.setText(color.name())
    color_button.clicked.connect(set_color);settings.addWidget(color_button)
    reset=QtWidgets.QPushButton('重置笔刷')
    def reset_brush():
        dialog.size.setValue(40);shape.setCurrentIndex(0)
        for key,value in [('hardness',100),('brush_opacity',100),('spacing',10)]:dialog.brush_sliders[key].setValue(value)
    reset.clicked.connect(reset_brush);settings.addWidget(reset)
    tolerance_title=QtWidgets.QLabel('选区容差');settings.addWidget(tolerance_title)
    tolerance=QtWidgets.QSlider(QtCore.Qt.Horizontal);tolerance.setRange(0,255);tolerance.setValue(30)
    tolerance.valueChanged.connect(lambda value:setattr(canvas,'tolerance',value));settings.addWidget(tolerance)
    algorithm=RhEnumComboBox();algorithm.addItems(['HSL','LAB','RGB']);algorithm.currentTextChanged.connect(lambda value:setattr(canvas,'color_mode',value));settings.addWidget(algorithm)
    mask_controls.extend([tolerance_title,tolerance,algorithm])
    settings.addWidget(QtWidgets.QLabel('图层'))
    layer=RhEnumComboBox();layer.addItem('编辑遮罩层','mask');layer.addItem('编辑绘画层','paint')
    def activate_layer():
        canvas.active_layer=layer.currentData()
        if canvas.tool!='erase':buttons[canvas.active_layer].click()
    layer.currentIndexChanged.connect(activate_layer);settings.addWidget(layer);layer.hide()
    dialog.layer_selector=layer
    for key,label in [('show_mask','显示遮罩'),('show_paint','显示绘画'),('show_base','显示原图')]:
        check=QtWidgets.QCheckBox(label);check.setChecked(True);check.toggled.connect(lambda value,k=key:(setattr(canvas,k,value),canvas.update()));settings.addWidget(check)
    blend=RhEnumComboBox()
    for label,key in [('彩色遮罩','color'),('黑色遮罩','black'),('白色遮罩','white'),('反相显示','negative')]:blend.addItem(label,key)
    blend.currentIndexChanged.connect(lambda:(setattr(canvas,'blend',blend.currentData()),setattr(canvas,'_overlay',None),canvas.update()));settings.addWidget(blend)
    opacity_title=QtWidgets.QLabel('遮罩显示透明度');settings.addWidget(opacity_title)
    opacity=QtWidgets.QSlider(QtCore.Qt.Horizontal);opacity.setRange(0,100);opacity.setValue(round(canvas.opacity*100))
    opacity.valueChanged.connect(lambda value:(setattr(canvas,'opacity',value/100),canvas.update()));settings.addWidget(opacity)
    mask_controls.extend([blend,opacity_title,opacity])
    settings.addStretch()
    dialog.status=QtWidgets.QLabel();layout.addWidget(dialog.status)
    hint=QtWidgets.QLabel('Ctrl+Z 撤销 · Ctrl+Y / Ctrl+Shift+Z 重做\nCtrl + 滚轮缩放 · 空格 / 中键平移 · Alt + 右键拖动调整笔刷\n运行时保存 RGBA 遮罩图像（MASK = 1 − Alpha）与绘画层；保留软边缘，旋转、镜像同步作用于所有图层。')
    hint.setWordWrap(True);layout.addWidget(hint)
    footer=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save|QtWidgets.QDialogButtonBox.Cancel)
    footer.button(footer.Save).setText('保存并使用');footer.button(footer.Cancel).setText('取消')
    footer.accepted.connect(dialog.save);footer.rejected.connect(dialog.reject);layout.addWidget(footer)
    for sequence,callback in [('Ctrl+Z',canvas.undo),('Ctrl+Y',canvas.redo),('Ctrl+Shift+Z',canvas.redo),('[',lambda:dialog.size.setValue(max(1,dialog.size.value()-5))),(']',lambda:dialog.size.setValue(min(1024,dialog.size.value()+5)))]:
        shortcut=QtWidgets.QShortcut(QtGui.QKeySequence(sequence),dialog);shortcut.activated.connect(callback)
    canvas.changed.connect(dialog.refresh);dialog.refresh()
    def switch_page(index):
        canvas.finish_stroke();paint=index==1
        canvas.active_layer='paint' if paint else 'mask';canvas.tool=canvas.active_layer
        buttons[canvas.active_layer].setChecked(True)
        for tool in ('mask','fill','color'):buttons[tool].setVisible(not paint)
        buttons['paint'].setVisible(paint)
        for control in mask_controls:control.setVisible(not paint)
        path_rows['mask_path_display'].setVisible(not paint);path_rows['paint_path_display'].setVisible(paint)
        importer.setToolTip('导入图像作为 RGBA 绘画层，替换当前绘画层；自动缩放并保留透明度' if paint else '导入遮罩图像')
        color_button.setText('绘画颜色 / 透明度…' if paint else '遮罩显示颜色…')
        canvas.update();dialog.refresh()
    tabs.currentChanged.connect(switch_page);switch_page(0)
    for button in dialog.findChildren(QtWidgets.QPushButton):button.setAutoDefault(False)
