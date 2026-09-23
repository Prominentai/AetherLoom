"""Image context actions and coordinated removal of finished NovelAI tasks."""
import copy
import os
import shutil
from pathlib import Path

from PyQt5 import QtWidgets


def _path_key(path):
    return os.path.normcase(os.path.abspath(path)) if isinstance(path, str) and path else ''


def task_for_record(page, record):
    identity, path = record.get('id'), _path_key(record.get('path'))
    if not identity and not path:
        return None
    for task in reversed(page._queue.tasks):
        for result in task.get('results') or []:
            if ((identity and result.get('id') == identity)
                    or (path and _path_key(result.get('path')) == path)):
                return task['id']
    return None


def _copy_image(page, record):
    from .preview import preview_image
    try:
        image = preview_image(record['path'])
        QtWidgets.QApplication.clipboard().setImage(image)
        page.status_message('图像已复制。')
    except (OSError, ValueError, KeyError) as error:
        page.status_message('图像无法复制：' + str(error))


def _save_copy(page, record):
    source = str(record.get('path', ''))
    target, _ = QtWidgets.QFileDialog.getSaveFileName(
        page, '另存图像副本', Path(source).name, '图像 (*' + Path(source).suffix + ');;所有文件 (*)')
    if target and _path_key(target) != _path_key(source):
        try:
            shutil.copy2(source, target)
            page.status_message('图像副本已保存。')
        except OSError as error:
            page.status_message('图像副本无法保存：' + str(error))


def create_preview_menu(page, preview=None):
    preview = preview or page.preview
    main = preview is page.preview
    record = copy.deepcopy(page._preview_image_record() if main else {'path': preview._requested_path})
    path = str(record.get('path') or '')
    available = bool(path and os.path.isfile(path))
    menu = QtWidgets.QMenu(page)
    menu.setFont(page.font())
    for title, slot in (
            ('打开原图', lambda: page.history.open(record)),
            ('打开文件位置', lambda: page.history.reveal(record)),
            ('复制图像', lambda: _copy_image(page, record)),
            ('复制文件路径', lambda: QtWidgets.QApplication.clipboard().setText(path)),
            ('另存图像副本…', lambda: _save_copy(page, record))):
        menu.addAction(title, slot).setEnabled(available)
    menu.addSeparator()
    menu.addAction('作为底图', lambda: page.set_input(path)).setEnabled(available)
    settings = record.get('settings') or {}
    menu.addAction('复用参数（保留种子和素材）',
                   lambda: page.reuse_parameters(record)).setEnabled(bool(settings))
    menu.addAction('回填种子', lambda: page.reuse_seed(record)).setEnabled(page._record_seed(record) is not None)
    menu.addSeparator()
    shown = not preview.snapshot_pixmap().isNull()
    menu.addAction('适应画面', preview.fit).setEnabled(shown)
    menu.addAction('原始尺寸（100%）', preview.actual).setEnabled(shown)
    menu.addAction('导入图像…', page.choose_input)
    identity = None if main and page._previewing_input else task_for_record(page, record)
    if identity:
        menu.addSeparator()
        menu.addAction('复制完整任务到主页', lambda: page.copy_task_to_page(identity))
        append_task_removal(menu, identity, page._queue.can_remove_task,
                            lambda task_id, files: remove_task(page, task_id, files))
    return menu


def append_task_removal(menu, identity, can_remove, callback):
    menu.addSeparator()
    removable = bool(can_remove and can_remove(identity))
    for label, delete_files in (('删除任务（保留本地文件）', False),
                                 ('删除任务并删除本地文件…', True)):
        action = menu.addAction(label, lambda checked=False, files=delete_files: callback(identity, files))
        action.setEnabled(removable)
        action.setToolTip('仅删除此任务生成的结果文件，保留输入素材。' if removable else
                          '请等待任务结束及后台保存完成后再删除。')


def remove_task(page, identity, delete_files=False):
    task = page._queue.get_task(identity)
    if task is None or not page._queue.can_remove_task(identity):
        page.status_message('请等待任务结束及后台保存完成后再删除。')
        return False
    records = copy.deepcopy(task.get('results') or [])
    paths = {_path_key(record.get('path')) for record in records} - {''}
    if delete_files and paths:
        answer = QtWidgets.QMessageBox.question(page, '删除任务与本地文件',
            f'将删除任务 #{task.get("index", "")} 及其 {len(paths)} 个结果文件。\n'
            '输入素材会保留；此操作不可撤销。',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return False
    error = None
    try:
        page._queue.remove_task(identity, delete_files=delete_files)
    except (OSError, ValueError, RuntimeError) as exc:
        error = exc
    # A partial filesystem failure keeps the task retryable. Remove only the
    # history references whose files have actually gone, never surviving ones.
    missing = [record for record in records if not os.path.isfile(str(record.get('path') or ''))]
    if delete_files:
        page.history.remove_records(records if error is None else missing)
    deleted_paths = {_path_key(record.get('path')) for record in missing} - {''}
    if error is None:
        page._stream_previews.pop(identity, None)
        page._enqueued_revisions.pop(identity, None)
        page._refreshed_tasks.discard(identity)
        if page._preview_task_id == identity or page._displayed_task_id == identity:
            page._follow_queue_preview = False
            page._preview_task_id = page._displayed_task_id = page._displayed_task_state = None
            page.task_panel.select_task(None)
            page._task_results = []
        if (page._result_view_context or {}).get('task_id') == identity:
            page._result_view_context = None
    if delete_files and deleted_paths:
        context = page._result_view_context or {}
        if _path_key((context.get('record') or {}).get('path')) in deleted_paths:
            page._result_view_context = None
        selected_deleted = _path_key((page._selected or {}).get('path')) in deleted_paths
        if selected_deleted:
            page._selected = None
        page._task_results = [record for record in page._task_results
                              if _path_key(record.get('path')) not in deleted_paths]
        if _path_key(page.comparison._requested_path) in deleted_paths:
            page.comparison.set_empty('结果文件已删除。')
            page.close_comparison()
        if _path_key(page.preview._requested_path) in deleted_paths or (
                page._previewing_input and _path_key(page._input) in deleted_paths) or (
                not page._previewing_input and selected_deleted):
            if page._task_results and not page._previewing_input:
                page._task_result_index = min(page._task_result_index, len(page._task_results) - 1)
                page._display_record(page._task_results[page._task_result_index])
            else:
                page.preview.set_empty('结果文件已删除。')
                page.result_label.setText('结果文件已删除')
                page.result_label.setToolTip('')
    page._task_result_index = min(page._task_result_index, max(0, len(page._task_results) - 1))
    selected_path = _path_key((page._selected or {}).get('path'))
    if selected_path:
        page._task_result_index = next((index for index, record in enumerate(page._task_results)
            if _path_key(record.get('path')) == selected_path), page._task_result_index)
    page._update_result_navigation()
    page.status_message('删除未完成，可重试：' + str(error) if error else
                        '任务及本地结果文件已删除。' if delete_files else '任务已删除，本地结果文件已保留。')
    return error is None
