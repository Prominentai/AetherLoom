"""Canvas-only preferences, commands and lightweight node-library history."""
import copy
import json

from PyQt5 import QtCore, QtGui, QtWidgets

from .preferences import PreferencesDialog, normalize_preferences, preferences_for


def choice_key(group, value):
    return str(group) + ':' + str(value)


class CanvasPreferencesMixin:
    def _init_preferences(self):
        self.canvas_preferences = preferences_for(self.owner)
        self._preference_save_timer = QtCore.QTimer(self)
        self._preference_save_timer.setSingleShot(True)
        self._preference_save_timer.setInterval(400)
        self._preference_save_timer.timeout.connect(self._persist_preferences)

    def _apply_preferences(self, values, *, persist=False):
        values = normalize_preferences(values)
        self.canvas_preferences = values
        self.view.set_preferences(values)
        self._autosave.setInterval(values['autosave_delay'])
        self._trim_history()
        self._prune_removed_runtime()
        self._refresh_library_preferences()
        for action, command in ((self.find_node_action, 'find'),
                                (self.fit_selected_action, 'fit_selected'),
                                (self.preferences_action, 'preferences')):
            label = action.property('baseLabel')
            shortcut = QtGui.QKeySequence(values['shortcuts'][command]).toString(QtGui.QKeySequence.NativeText)
            action.setText(label + ('\t' + shortcut if shortcut else ''))
        self.add_node_action.setToolTip('搜索并添加节点 · ' + values['shortcuts']['add'])
        if persist:
            self._persist_preferences()

    def _persist_preferences(self, defer=False):
        settings = getattr(self.owner, 'settings', None)
        if not isinstance(settings, dict):
            settings = {}
            self.owner.settings = settings
        settings['canvas_preferences'] = copy.deepcopy(self.canvas_preferences)
        self.settings = settings
        if defer:
            self._preference_save_timer.start()
            return
        self._preference_save_timer.stop()
        save = getattr(self.owner, '_save_settings', None)
        if save:
            save()

    def _open_preferences(self):
        dialog = PreferencesDialog(self.canvas_preferences, parent=self, colors=self.scene.colors)
        try:
            if dialog.exec_() == QtWidgets.QDialog.Accepted:
                self._apply_preferences(dialog.values(), persist=True)
                self._message('画布偏好已保存并生效。')
        finally:
            dialog.deleteLater()

    def _build_preference_actions(self):
        from .workspace_tools import align_selected, set_layout_locked, show_node_finder, show_node_diagnostics
        organize = QtWidgets.QMenu('整理节点', self)
        self.organize_menu = organize
        for title, mode in [('左对齐', 'left'), ('水平居中', 'hcenter'), ('右对齐', 'right'),
                            ('顶部对齐', 'top'), ('垂直居中', 'vcenter'), ('底部对齐', 'bottom'),
                            ('水平等距分布', 'distribute_h'), ('垂直等距分布', 'distribute_v')]:
            organize.addAction(title, lambda checked=False, m=mode: align_selected(self, m))
        organize.addSeparator()
        organize.addAction('锁定位置与大小', lambda: set_layout_locked(self, True))
        organize.addAction('解除布局锁定', lambda: set_layout_locked(self, False))
        self.edit_menu.addSeparator()
        self.edit_menu.addMenu(organize)
        self.diagnostics_action = self.edit_menu.addAction('查看运行原因…', lambda: show_node_diagnostics(self))
        self.find_node_action = self.view_menu.addAction('查找画布节点…', lambda: show_node_finder(self))
        self.fit_selected_action = self.view_menu.addAction('适应所选节点', lambda: self.view.fit_selected())
        self.view_menu.addSeparator()
        self.preferences_action = self.view_menu.addAction('画布偏好…', self._open_preferences)
        for action in (self.find_node_action, self.fit_selected_action, self.preferences_action):
            action.setProperty('baseLabel', action.text())

    def _preference_action(self, action):
        from .workspace_tools import set_layout_locked, show_node_finder, show_node_diagnostics
        callbacks = {
            'preferences': self._open_preferences,
            'find': lambda: show_node_finder(self),
            'fit_selected': self.view.fit_selected,
            'run': self.run_canvas,
            'stop': self.stop_canvas,
            'add': lambda: self._show_node_search(
                self.view._last_scene_pos or self.view.mapToScene(self.view.available_rect().center().toPoint())),
            'lock_layout': lambda: set_layout_locked(self, True),
            'unlock_layout': lambda: set_layout_locked(self, False),
            'diagnostics': lambda: show_node_diagnostics(self),
        }
        if action in callbacks:
            callbacks[action]()
            return True
        return False

    def _trim_history(self):
        """Cap both step count and retained serialized document size (32 MiB)."""
        limit = self.canvas_preferences['undo_limit']
        del self._undo[:-limit]
        del self._redo[:-limit]
        previous = getattr(self, '_history_weights', {})
        weights = {}
        for snapshot in self._undo + self._redo:
            old = previous.get(id(snapshot))
            size = old[1] if old and old[0] is snapshot else len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8'))
            weights[id(snapshot)] = (snapshot, size)
        total = sum(value[1] for value in weights.values())
        while total > 32 * 1024 * 1024 and len(weights) > 1:
            # Keep the newest undo and redo steps closest to the current state.
            history = self._undo if len(self._undo) >= len(self._redo) else self._redo
            discarded = history.pop(0)
            total -= weights.pop(id(discarded))[1]
        self._history_weights = weights

    def _remember_choice(self, choice):
        key = choice_key(choice['group'], choice['value'])
        recent = self.canvas_preferences['recent']
        self.canvas_preferences['recent'] = [key] + [item for item in recent if item != key][:19]
        self._refresh_library_preferences()
        self._persist_preferences(defer=True)

    def _toggle_favorite(self, group, value):
        key = choice_key(group, value)
        favorites = self.canvas_preferences['favorites']
        if key in favorites:
            favorites.remove(key)
        else:
            favorites.append(key)
        self._refresh_library_preferences()
        self._persist_preferences(defer=True)

    def _refresh_library_preferences(self):
        library = getattr(self, 'library_list', None)
        if library is not None:
            library.set_preferences(self.canvas_preferences)
            self._filter_library()

    def _library_context_menu(self, point):
        item = self.library_list.itemAt(point)
        choice = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        if not choice:
            return
        menu = QtWidgets.QMenu(self)
        from aetherloom_core.ui.menus import stylesheet
        menu.setStyleSheet(stylesheet(getattr(self.owner, '_theme_mode', 'dark'), self.font()))
        favorite = choice_key(*choice) in self.canvas_preferences['favorites']
        add = menu.addAction('添加节点')
        star = menu.addAction('取消收藏' if favorite else '收藏节点')
        try:
            chosen = menu.exec_(self.library_list.viewport().mapToGlobal(point))
            if chosen is add:
                self._library_add(item)
            elif chosen is star:
                self._toggle_favorite(*choice)
        finally:
            menu.deleteLater()
