"""Window geometry, theme, scale, and presentation behavior."""
from aetherloom_core.resources import PLAY_BUTTON_SVG
from PyQt5.QtCore import Qt
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.platform_utils import _set_native_titlebar_dark
from aetherloom_core.platform_utils import _svg_to_icon
from aetherloom_core.paths import current_dir, SOURCE_ROOT
import os


class PresentationMixin:
    def _restore_window_geometry(self, initial=False):
        try:
            saved_window = (self.settings or {}).get('window') or {}
            screen = self._pick_screen_for_restore(saved_window)
            missing_saved_screen = bool(saved_window.get('screen_name') and
                                        (screen is None or screen.name() != saved_window['screen_name']))
            if screen is None:
                screen = QtWidgets.QApplication.primaryScreen()
                missing_saved_screen = True
            if screen is None:
                return
            avail = screen.availableGeometry()
            target_rect = None
            saved_w = saved_window.get('width')
            saved_h = saved_window.get('height')
            saved_sw = saved_window.get('screen_width')
            saved_sh = saved_window.get('screen_height')
            saved_local_x = saved_window.get('local_x')
            saved_local_y = saved_window.get('local_y')
            if saved_w and saved_h:
                target_rect = self._compute_scaled_geometry(
                    saved_w,
                    saved_h,
                    saved_sw,
                    saved_sh,
                    saved_local_x,
                    saved_local_y,
                    avail
                )
            if target_rect is None:
                missing_saved_screen = True
                default_w = int(avail.width() * 0.82)
                default_h = int(avail.height() * 0.85)
                default_w = max(min(default_w, avail.width()), min(1100, avail.width()))
                default_h = max(min(default_h, avail.height()), min(900, avail.height()))
                x = avail.x() + (avail.width() - default_w) // 2
                y = avail.y() + (avail.height() - default_h) // 2
                target_rect = QtCore.QRect(x, y, default_w, default_h)
            # clamp requested geometry to respect widget minimums and available screen
            try:
                min_req_w = 0
                min_req_h = 0
                try:
                    ms = self.minimumSize()
                    min_req_w = int(ms.width() or 0)
                    min_req_h = int(ms.height() or 0)
                except Exception:
                    min_req_w = 0
                    min_req_h = 0
                # ensure we don't request larger than available screen
                req_w = max(int(target_rect.width()), min_req_w)
                req_h = max(int(target_rect.height()), min_req_h)
                # if minimum requirements exceed available, cap to available to avoid failure
                req_w = min(req_w, avail.width())
                req_h = min(req_h, avail.height())
                # recompute x/y to keep window inside available geometry
                x = int(target_rect.x())
                y = int(target_rect.y())
                x = max(avail.x(), min(x, avail.x() + avail.width() - req_w))
                y = max(avail.y(), min(y, avail.y() + avail.height() - req_h))
                final_rect = QtCore.QRect(x, y, req_w, req_h)
                self.setGeometry(final_rect)
            except Exception:
                try:
                    self.setGeometry(target_rect)
                except Exception:
                    pass
            self._apply_ui_scale(self._calc_scale_from_avail(avail))
            self._ensure_visible_geometry(force_center=missing_saved_screen, screen=screen)
            self._active_screen_size = (avail.width(), avail.height())
            self._active_screen_name = screen.name()
            if saved_window.get('maximized'):
                self._pending_restore_maximized = True
        except Exception:
            pass


    def _pick_screen_for_restore(self, saved_window):
        try:
            screens = QtWidgets.QApplication.screens()
            if not screens:
                return None
            saved_name = (saved_window or {}).get('screen_name')
            if saved_name:
                for sc in screens:
                    try:
                        if sc.name() == saved_name:
                            return sc
                    except Exception:
                        continue
            cursor_pos = QtGui.QCursor.pos()
            target = QtWidgets.QApplication.screenAt(cursor_pos)
            if target is not None:
                return target
            return QtWidgets.QApplication.primaryScreen()
        except Exception:
            return QtWidgets.QApplication.primaryScreen()


    def _compute_scaled_geometry(self, width, height, saved_sw, saved_sh, saved_local_x, saved_local_y, target_avail):
        try:
            width = int(width)
            height = int(height)
            target_w = max(720, min(target_avail.width(), int(width * self._scale_factor(saved_sw, target_avail.width()))))
            target_h = max(520, min(target_avail.height(), int(height * self._scale_factor(saved_sh, target_avail.height()))))
            target_w = min(target_w, target_avail.width())
            target_h = min(target_h, target_avail.height())
            x = target_avail.x() + (target_avail.width() - target_w) // 2
            y = target_avail.y() + (target_avail.height() - target_h) // 2
            if saved_local_x is not None and saved_sw:
                try:
                    ratio_x = float(saved_local_x) / max(1.0, float(saved_sw) - width)
                    x = target_avail.x() + int(ratio_x * (target_avail.width() - target_w))
                except Exception:
                    pass
            if saved_local_y is not None and saved_sh:
                try:
                    ratio_y = float(saved_local_y) / max(1.0, float(saved_sh) - height)
                    y = target_avail.y() + int(ratio_y * (target_avail.height() - target_h))
                except Exception:
                    pass
            x = max(target_avail.x(), min(x, target_avail.x() + target_avail.width() - target_w))
            y = max(target_avail.y(), min(y, target_avail.y() + target_avail.height() - target_h))
            return QtCore.QRect(x, y, target_w, target_h)
        except Exception:
            return None


    def _scale_factor(self, saved_size, target_size):
        try:
            saved = float(saved_size or 0)
            if saved <= 0:
                return 1.0
            factor = float(target_size) / saved
            return max(0.4, min(factor, 1.4))
        except Exception:
            return 1.0


    def _calc_scale_from_avail(self, avail_rect):
        try:
            if avail_rect is None:
                return 1.0
            ref_w, ref_h = 1920.0, 1080.0
            scale_w = avail_rect.width() / ref_w
            scale_h = avail_rect.height() / ref_h
            scale = min(scale_w, scale_h)
            return max(0.8, min(scale, 1.15))
        except Exception:
            return 1.0


    def _apply_ui_scale(self, scale):
        try:
            if scale is None:
                return
            scale = max(0.5, min(scale, 1.8))
            self._ui_scale_factor = scale
            base_font = getattr(self, '_base_font_point', 11.5)
            try:
                font = QtGui.QFont(self.font())
            except Exception:
                font = self.font()
            if font is not None:
                try:
                    font.setPointSizeF(max(8.5, base_font * scale))
                    self.setFont(font)
                except Exception:
                    pass
            # Preserve the sidebar's proportions without shrinking styled labels.
            try:
                win_w = int(self.width() or 0)
            except Exception:
                win_w = 0
            if not win_w or win_w < 200:
                try:
                    screen = QtWidgets.QApplication.primaryScreen()
                    win_w = int(screen.availableGeometry().width()) if screen is not None else 1200
                except Exception:
                    win_w = 1200
            base_frac = float(getattr(self, '_sidebar_base_frac', 0.12))
            try:
                collapsed = bool(getattr(self, '_sidebar_collapsed', False) or
                                 getattr(self, '_sidebar_auto_collapsed', False))
                override = getattr(self, '_sidebar_auto_override', None)
                if override is not None:
                    collapsed = override
                self._sidebar_effective_collapsed = collapsed
                button_style = Qt.ToolButtonIconOnly if collapsed else Qt.ToolButtonTextUnderIcon
                base_height = getattr(self, '_sidebar_button_height_base', 56)
                base_icon = getattr(self, '_sidebar_icon_px_base', 28)
                button_height = max(44, int(base_height * scale))
                icon_px = max(20, int(base_icon * scale))
                if collapsed:
                    button_height = max(44, icon_px + 20)
                needed_button_width = 0
                for btn in getattr(self, '_sidebar_buttons', []):
                    if btn is None:
                        continue
                    btn.setToolButtonStyle(button_style)
                    btn.setIconSize(QtCore.QSize(icon_px, icon_px))
                    btn.ensurePolished()
                    # Qt's hint includes the actual theme font, icon, text gap and
                    # stylesheet padding. Scaling the font again underestimates it.
                    hint = btn.sizeHint()
                    btn.setFixedHeight(max(button_height, hint.height()))
                    needed_button_width = max(needed_button_width, hint.width())
                margins = self.sidebar_frame.layout().contentsMargins()
                required_width = needed_button_width + margins.left() + margins.right()
                required_width += self.sidebar_frame.frameWidth() * 2
                if collapsed:
                    collapsed_frac = float(getattr(self, '_sidebar_collapsed_min_frac', 0.04))
                    sidebar_width = max(required_width, int(win_w * collapsed_frac),
                                        int(getattr(self, '_sidebar_collapsed_min_px', 80)))
                else:
                    min_frac = float(getattr(self, '_sidebar_min_frac', 0.06))
                    preferred_width = max(140, int(win_w * min_frac), int(win_w * base_frac * scale))
                    max_frac = float(getattr(self, '_sidebar_max_fraction', 0.12))
                    cap = min(int(getattr(self, '_sidebar_max_px', 420)), max(160, int(win_w * max_frac)))
                    sidebar_width = max(required_width, min(preferred_width, cap))
                self.sidebar_frame.setFixedWidth(sidebar_width)
                if getattr(self, 'sidebar_toggle_btn', None):
                    self.sidebar_toggle_btn.setText('\u203A' if collapsed else '\u2039')
                try:
                    # scale theme toggle button/icon alongside sidebar
                    toggle_btn = getattr(self, 'theme_toggle_btn', None)
                    if toggle_btn is not None:
                        base_toggle = getattr(self, '_theme_toggle_size_base', 52)
                        base_toggle_icon = getattr(self, '_theme_toggle_icon_px_base', 32)
                        sz = max(42, int(base_toggle * scale))
                        icon_sz = max(24, int(base_toggle_icon * scale))
                        toggle_btn.setFixedSize(QtCore.QSize(sz, sz))
                        toggle_btn.setIconSize(QtCore.QSize(icon_sz, icon_sz))
                except Exception:
                    pass
            except Exception:
                pass
            try:
                brand = getattr(self, 'sidebar_brand_label', None)
                if brand is not None:
                    font = QtGui.QFont(brand.font())
                    font.setPointSizeF(max(11.0, 14.0 * scale))
                    brand.setFont(font)
            except Exception:
                pass
            try:
                if hasattr(self, 'file_list') and self.file_list is not None:
                    base_icon = getattr(self, '_file_list_icon_base', 120)
                    icon_px = max(72, int(base_icon * scale))
                    self.file_list.setIconSize(QtCore.QSize(icon_px, icon_px))
                    self.file_list.setSpacing(max(4, int(6 * scale)))
            except Exception:
                pass
            try:
                min_w = max(240, int(self._preview_min_base.width() * scale))
                min_h = max(180, int(self._preview_min_base.height() * scale))
                for _lbl in (getattr(self, 'orig_view_grc', None), getattr(self, 'orig_view_sst', None)):
                    if _lbl is not None:
                        _lbl.setMinimumSize(min_w, min_h)
            except Exception:
                pass
            try:
                out_w = max(360, int(self._output_min_base.width() * scale))
                out_h = max(260, int(self._output_min_base.height() * scale))
                if hasattr(self, 'output_view') and self.output_view is not None:
                    self.output_view.setMinimumSize(out_w, out_h)
            except Exception:
                pass
            try:
                btn = getattr(self, 'output_play_btn', None)
                if btn is not None:
                    base_btn = getattr(self, '_play_btn_size_base', 152)
                    base_icon = getattr(self, '_play_icon_px_base', 100)
                    btn_sz = max(100, int(base_btn * scale))
                    icon_sz = max(72, int(base_icon * scale))
                    btn.setFixedSize(QtCore.QSize(btn_sz, btn_sz))
                    btn.setIconSize(QtCore.QSize(icon_sz, icon_sz))
                    self._refresh_output_play_icon(icon_sz)
            except Exception:
                pass
            try:
                if hasattr(self, 'theme_toggle_btn') and self.theme_toggle_btn is not None:
                    btn_h = max(28, int(34 * scale))
                    self.theme_toggle_btn.setFixedHeight(btn_h)
                    f = QtGui.QFont(self.theme_toggle_btn.font())
                    f.setPointSizeF(max(8.0, 10.0 * scale))
                    self.theme_toggle_btn.setFont(f)
            except Exception:
                pass
            try:
                slider_w = int(420 * scale)
                slider_h = int(28 * scale)
                if hasattr(self, 'thumb_size_slider') and self.thumb_size_slider is not None:
                    self.thumb_size_slider.setMinimumWidth(80)
                    self.thumb_size_slider.setMaximumWidth(min(420, max(200, slider_w)))
                    self.thumb_size_slider.setFixedHeight(max(20, slider_h))
            except Exception:
                pass
            try:
                if hasattr(self, 'thumb_size_spin') and self.thumb_size_spin is not None:
                    self.thumb_size_spin.setFixedWidth(max(110, int(140 * scale)))
            except Exception:
                pass
            try:
                self._apply_filter_controls_scale(scale)
            except Exception:
                pass
            try:
                self._apply_control_group_font(scale)
            except Exception:
                pass
        except Exception:
            pass


    def _set_sidebar_collapsed(self, collapsed: bool):
        """Collapse or expand the sidebar. When collapsed the sidebar shows icons only."""
        try:
            self._sidebar_collapsed = bool(collapsed)
            self._sidebar_auto_override = (bool(collapsed) if
                                           getattr(self, '_sidebar_auto_collapsed', False) else None)
            # force immediate UI update
            try:
                self._apply_ui_scale(getattr(self, '_ui_scale_factor', 1.0))
            except Exception:
                # fallback adjustments if _apply_ui_scale fails
                try:
                    if self._sidebar_collapsed:
                        icon_px = max(20, int(getattr(self, '_sidebar_icon_px_base', 28) * getattr(self, '_ui_scale_factor', 1.0)))
                        min_w = max(int(getattr(self, '_sidebar_collapsed_min_px', 80)), int(icon_px + 20))
                        try:
                            self.sidebar_frame.setFixedWidth(min_w)
                        except Exception:
                            pass
                        for btn in getattr(self, '_sidebar_buttons', []) or []:
                            try:
                                btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
                            except Exception:
                                pass
                        try:
                            if getattr(self, 'sidebar_toggle_btn', None):
                                self.sidebar_toggle_btn.setText('\u203A')
                        except Exception:
                            pass
                    else:
                        for btn in getattr(self, '_sidebar_buttons', []) or []:
                            try:
                                btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                            except Exception:
                                pass
                        try:
                            if getattr(self, 'sidebar_toggle_btn', None):
                                self.sidebar_toggle_btn.setText('\u2039')
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass


    def _fit_sidebar_to_theme(self):
        """Recompute from current hints, allowing the sidebar to shrink again."""
        self._apply_ui_scale(getattr(self, '_ui_scale_factor', 1.0))


    def _apply_filter_controls_scale(self, scale=None):
        try:
            if scale is None:
                scale = getattr(self, '_ui_scale_factor', 1.0)
            scale = max(0.5, min(scale, 1.8))
            add_btn = getattr(self, 'local_filter_add_btn', None)
            clear_btn = getattr(self, 'local_filter_clear_btn', None)
            dropdown = getattr(self, 'local_filter_dropdown', None)
            if add_btn is not None:
                add_h = max(30, int(38 * scale))
                add_btn.setFixedHeight(add_h)
                add_btn.setMinimumWidth(max(110, int(126 * scale)))
                radius = max(10, int(16 * scale))
                pad_v = max(4, int(6 * scale))
                pad_h = max(10, int(16 * scale))
                add_btn.setStyleSheet(
                    f'QPushButton#localFilterAddButton {{ border-radius: {radius}px; padding: {pad_v}px {pad_h}px; background: #2265d8; color: #ffffff; font-weight: 600; }} '
                    f'QPushButton#localFilterAddButton:hover {{ background: #1b52b5; }}'
                )
            if clear_btn is not None:
                clear_h = max(28, int(36 * scale))
                clear_btn.setFixedHeight(clear_h)
                clear_btn.setMinimumWidth(max(96, int(112 * scale)))
                radius = max(10, int(14 * scale))
                pad_v = max(4, int(6 * scale))
                pad_h = max(10, int(16 * scale))
                clear_btn.setStyleSheet(
                    f'QPushButton#localFilterClearButton {{ border-radius: {radius}px; padding: {pad_v}px {pad_h}px; background: rgba(244,67,54,0.85); color: #ffffff; font-weight: 600; }} '
                    f'QPushButton#localFilterClearButton:hover {{ background: rgba(229,57,53,0.95); }}'
                )
            if dropdown is not None:
                drop_h = max(28, int(36 * scale))
                dropdown.setFixedHeight(drop_h)
                dropdown.setMinimumWidth(max(160, int(220 * scale)))
                radius = max(10, int(12 * scale))
                pad_v = max(3, int(4 * scale))
                pad_h = max(10, int(12 * scale))
                drop_width = max(20, int(28 * scale))
                dropdown.setStyleSheet(
                    f'QComboBox {{ border: 1px solid #5c6bc0; border-radius: {radius}px; padding: {pad_v}px {pad_h}px; background: rgba(17,17,19,0.4); }} '
                    f'QComboBox:focus {{ border-color: #2265d8; }} '
                    f'QComboBox::drop-down {{ width: {drop_width}px; border-left: 1px solid rgba(255,255,255,0.08); }}'
                )
        except Exception:
            pass


    def _apply_control_group_font(self, scale=None):
        try:
            group = getattr(self, 'decode_control_group', None)
            template = getattr(self, '_control_group_css_template', None)
            if group is None or not template:
                return
            if scale is None:
                scale = getattr(self, '_ui_scale_factor', 1.0)
            scale = max(0.5, min(scale, 1.8))
            base = getattr(self, '_control_panel_font_base', 11.5)
            size = max(10.0, base * scale)
            group.setStyleSheet(template.format(size=f'{size:.2f}'))
        except Exception:
            pass


    def _update_filter_clear_visibility(self):
        try:
            clear_btn = getattr(self, 'local_filter_clear_btn', None)
            if clear_btn is None:
                return
            rows = getattr(self, '_local_filter_rows', []) or []
            clear_btn.setVisible(len(rows) >= 2)
        except Exception:
            pass


    def _ensure_visible_geometry(self, force_center=False, screen=None):
        # The window manager owns maximized/fullscreen geometry. Correcting it here
        # would overwrite the normal restore rectangle (and can trigger loops).
        if self.isMaximized() or self.isFullScreen() or self.isMinimized():
            return
        screens = QtWidgets.QApplication.screens()
        if not screens:
            return
        if screen is None:
            screen = QtWidgets.QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = max(screens, key=lambda sc: self.frameGeometry().intersected(
                sc.availableGeometry()).width() * self.frameGeometry().intersected(
                sc.availableGeometry()).height())
        avail = screen.availableGeometry()
        self._geometry_fit_target = QtCore.QRect(avail)
        self._fit_available_geometry(avail, force_center)


    def _fit_available_geometry(self, avail, force_center=False, settled=False):
        if (getattr(self, '_closing', False) or self.isMaximized() or
                self.isMinimized() or self.isFullScreen()):
            return
        geom = QtCore.QRect(self.geometry())
        handle = self.windowHandle()
        margins = handle.frameMargins() if handle is not None else QtCore.QMargins()
        client_area = avail.adjusted(margins.left(), margins.top(),
                                     -margins.right(), -margins.bottom())
        if client_area.isEmpty():
            return
        self.setMinimumSize(min(720, client_area.width()), min(480, client_area.height()))
        width = min(max(geom.width(), self.minimumWidth()), client_area.width())
        height = min(max(geom.height(), self.minimumHeight()), client_area.height())
        if force_center:
            x = client_area.x() + (client_area.width() - width) // 2
            y = client_area.y() + (client_area.height() - height) // 2
        else:
            x = max(client_area.left(), min(geom.x(), client_area.right() - width + 1))
            y = max(client_area.top(), min(geom.y(), client_area.bottom() - height + 1))
        fitted = QtCore.QRect(x, y, width, height)
        if fitted != geom:
            self.setGeometry(fitted)
            # Native frame margins can become available during the first move
            # (notably when restoring onto a screen with negative coordinates).
            offset = fitted.topLeft() - self.geometry().topLeft()
            if not offset.isNull():
                self.move(self.pos() + offset)
            if not settled:
                QtCore.QTimer.singleShot(16, lambda: self._fit_available_geometry(avail, settled=True)
                                        if getattr(self, '_geometry_fit_target', None) == avail else None)


    def _ensure_screen_signal(self):
        try:
            if getattr(self, '_screen_signal_bound', False):
                return
            handle = self.windowHandle()
            if handle is None:
                return
            handle.screenChanged.connect(self._handle_screen_change)
            self._screen_signal_bound = True
            self._bind_screen_geometry_signal(handle.screen())
        except Exception:
            pass


    def _handle_screen_change(self, screen):
        if screen is None:
            return
        self._bind_screen_geometry_signal(screen)
        avail = screen.availableGeometry()
        self._active_screen_size = (avail.width(), avail.height())
        self._active_screen_name = screen.name()
        self._apply_ui_scale(self._calc_scale_from_avail(avail))
        # Qt already adjusts logical geometry on a DPI transition. Scaling it a
        # second time made repeated monitor moves grow/shrink the window.
        self._ensure_visible_geometry(screen=screen)


    def _bind_screen_geometry_signal(self, screen):
        try:
            if screen is None:
                return
            if getattr(self, '_bound_screen', None) is screen:
                return
            if getattr(self, '_bound_screen', None) is not None:
                try:
                    self._bound_screen.geometryChanged.disconnect(self._handle_screen_geometry_changed)
                except Exception:
                    pass
                try:
                    self._bound_screen.availableGeometryChanged.disconnect(self._handle_screen_geometry_changed)
                except Exception:
                    pass
            self._bound_screen = screen
            try:
                screen.geometryChanged.connect(self._handle_screen_geometry_changed)
            except Exception:
                pass
            try:
                screen.availableGeometryChanged.connect(self._handle_screen_geometry_changed)
            except Exception:
                pass
        except Exception:
            pass


    def _handle_screen_geometry_changed(self, *args):
        try:
            screen = getattr(self, '_bound_screen', None)
            if screen is None:
                handle = self.windowHandle()
                screen = handle.screen() if handle is not None else None
            if screen is None:
                return
            avail = screen.availableGeometry()
            self._active_screen_size = (avail.width(), avail.height())
            self._active_screen_name = screen.name()
            self._apply_ui_scale(self._calc_scale_from_avail(avail))
            self._ensure_visible_geometry(screen=screen)
        except Exception:
            pass


    def showEvent(self, event):
        try:
            super().showEvent(event)
        except Exception:
            event.accept()
        QtCore.QTimer.singleShot(0, self._ensure_screen_signal)
        if getattr(self, '_pending_restore_maximized', False):
            QtCore.QTimer.singleShot(0, lambda: self.setWindowState(self.windowState() | QtCore.Qt.WindowMaximized))
            self._pending_restore_maximized = False
        self._ensure_visible_geometry()


    def _build_theme_palettes(self):
        light_css = '''
            QWidget { font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", Roboto, Arial, Helvetica, sans-serif; font-size: 11.5pt; color: #1e2433; }
            QMainWindow { background: #f5f6fb; }
            QFrame#sidebarFrame { background: #ffffff; border: 1px solid #e1e5f0; border-radius: 18px; }
            QToolButton { background: transparent; border: none; color: #1f2430; font-size: 10pt; font-weight: 600; padding-top: 6px; padding-bottom: 10px; }
            QToolButton#sidebarButton { padding: 10px 14px; border-radius: 16px; font-size: 11pt; font-weight: 700; text-align: left; }
            QToolButton:checked { background: rgba(34, 101, 216, 0.12); border-radius: 14px; }
            QToolButton:hover { background: rgba(34, 101, 216, 0.08); }
            QLabel#sidebarBrand { font-size: 14pt; font-weight: 800; letter-spacing: 0.5px; color: #0f1626; padding: 0 0 6px 2px; }
            QListWidget, QTextEdit, QLineEdit, QSpinBox, QComboBox, QDateEdit { background: #ffffff; border: 1px solid #dfe3ec; border-radius: 8px; padding: 6px; }
            QListWidget { selection-background-color: rgba(34, 101, 216, 0.08); }
            QGroupBox { font-weight: 600; border: 1px solid #dfe3ec; border-radius: 10px; margin-top: 8px; padding: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 3px 0 3px; }
            QPushButton { background: #2265d8; color: white; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
            QPushButton:hover { background: #1b52b5; }
            QPushButton:disabled { background: #c8d3eb; color: #ffffff; }
            QPushButton#themeToggleButton { background: transparent; border: 1px solid #cdd4e3; color: #1f2430; border-radius: 18px; padding: 6px 12px; font-size: 10pt; }
            QPushButton#themeToggleButton:hover { background: rgba(34, 101, 216, 0.08); }
            QLabel#previewLabel { background: #ebeff7; border: 1px solid #d1d8e8; border-radius: 12px; }
            QTextEdit { background: #ffffff; }
            QWidget#settings_page_root { background: #f6f8fc; }
            QFrame#settingsHeroFrame { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #ecf1ff); border: 1px solid rgba(34, 101, 216, 0.25); border-radius: 14px; padding: 18px; }
            QLabel#settingsHeroTitle { font-size: 22px; font-weight: 700; color: #0f1626; }
            QLabel#settingsHeroSubtitle { color: #3c4761; font-size: 13px; }
            QLabel#settingsHeroHint { color: #2265d8; font-size: 12px; }
            QFrame#settingsCard { background: #ffffff; border: 1px solid rgba(17, 34, 68, 0.08); border-radius: 12px; }
            QLabel#settingsCardTitle { font-size: 17px; font-weight: 700; color: #0f1626; }
            QLabel#settingsHint { color: #5b6274; }
            QLineEdit#settingsPathInput { background: #fefefe; border: 1px solid #cfd7ea; border-radius: 8px; padding: 8px 10px; color: #0f1626; }
            QLineEdit#settingsPathInput:focus { border: 1px solid #2265d8; background: #ffffff; }
            QTabWidget::pane { border: none; background: #ebeff7; }
            QTabBar::tab { background: #eef1fb; color: #1f2430; border: 1px solid #d4daec; border-bottom: none; border-top-left-radius: 10px; border-top-right-radius: 10px; padding: 6px 18px; margin-right: 6px; }
            QTabBar::tab:selected { background: #2265d8; color: #ffffff; border-color: #1b52b5; }
            QTabBar::tab:hover { background: #dfe6fb; }
            QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
            QScrollBar::handle:vertical { background: qradialgradient(cx:0.5, cy:0.5, radius:0.7, fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.32 rgba(34, 101, 216, 0.55), stop:1 rgba(34, 101, 216, 0.45)); border-radius: 6px; min-height: 28px; }
            QScrollBar::handle:vertical:hover { background: qradialgradient(cx:0.5, cy:0.5, radius:0.7, fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.32 rgba(34, 101, 216, 0.75), stop:1 rgba(34, 101, 216, 0.65)); }
            QScrollBar::handle:vertical:pressed { background: qradialgradient(cx:0.5, cy:0.5, radius:0.7, fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.32 rgba(27, 82, 181, 0.95), stop:1 rgba(27, 82, 181, 0.85)); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; width: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        '''

        dark_css = '''
            QWidget { font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", Roboto, Arial, Helvetica, sans-serif; font-size: 11.5pt; color: #dfe7ff; background: #0b101a; }
            QMainWindow { background: #0b101a; }
            QFrame#sidebarFrame { background: #111726; border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 18px; }
            QToolButton { background: transparent; border: none; color: #f0f4ff; font-size: 10pt; font-weight: 600; padding-top: 6px; padding-bottom: 10px; }
            QToolButton#sidebarButton { padding: 10px 14px; border-radius: 16px; font-size: 11pt; font-weight: 700; text-align: left; }
            QToolButton:checked { background: rgba(255, 255, 255, 0.08); border-radius: 14px; }
            QToolButton:hover { background: rgba(255, 255, 255, 0.05); }
            QLabel#sidebarBrand { font-size: 14pt; font-weight: 800; letter-spacing: 0.5px; color: #f5f7ff; padding: 0 0 6px 2px; }
            QListWidget, QTextEdit, QLineEdit, QSpinBox, QComboBox, QDateEdit { background: rgba(16, 22, 33, 0.9); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 6px; }
            QListWidget { selection-background-color: rgba(43, 139, 213, 0.18); }
            QGroupBox { font-weight: 600; border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 10px; margin-top: 8px; padding: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 3px 0 3px; }
            QPushButton { background: #2b8bd5; color: white; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
            QPushButton:hover { background: #349ff3; }
            QPushButton:disabled { background: rgba(255, 255, 255, 0.1); color: rgba(255, 255, 255, 0.4); }
            QPushButton#themeToggleButton { background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.18); color: #f2f5ff; border-radius: 18px; padding: 6px 12px; font-size: 10pt; }
            QPushButton#themeToggleButton:hover { background: rgba(53, 167, 255, 0.18); }
            QLabel#previewLabel { background: #0f1720; border: 1px solid #1f2a37; border-radius: 12px; }
            QTextEdit { background: rgba(16, 22, 33, 0.9); }
            QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
            QScrollBar::handle:vertical { background: qradialgradient(cx:0.5, cy:0.5, radius:0.7, fx:0.5, fy:0.5, stop:0 rgba(255,255,255,0.9), stop:0.32 rgba(86, 132, 255, 0.6), stop:1 rgba(86, 132, 255, 0.45)); border-radius: 6px; min-height: 28px; }
            QScrollBar::handle:vertical:hover { background: qradialgradient(cx:0.5, cy:0.5, radius:0.7, fx:0.5, fy:0.5, stop:0 rgba(255,255,255,0.95), stop:0.32 rgba(86, 132, 255, 0.8), stop:1 rgba(86, 132, 255, 0.7)); }
            QScrollBar::handle:vertical:pressed { background: qradialgradient(cx:0.5, cy:0.5, radius:0.7, fx:0.5, fy:0.5, stop:0 rgba(255,255,255,0.95), stop:0.32 rgba(52, 159, 243, 0.95), stop:1 rgba(52, 159, 243, 0.9)); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; width: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            QWidget#settings_page_root { background: #0f1117; }
            QFrame#settingsHeroFrame { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1f2a3a, stop:1 #131b27); border: 1px solid rgba(90, 142, 255, 0.55); border-radius: 12px; padding: 18px; }
            QLabel#settingsHeroTitle { font-size: 22px; font-weight: 700; color: #f5f7ff; }
            QLabel#settingsHeroSubtitle { color: #d3d9ff; font-size: 13px; }
            QLabel#settingsHeroHint { color: #72d9fb; font-size: 12px; }
            QFrame#settingsCard { background: #151a23; border: 1px solid rgba(90, 100, 140, 0.55); border-radius: 12px; }
            QLabel#settingsCardTitle { font-size: 17px; font-weight: 700; color: #f2f5ff; }
            QLabel#settingsHint { color: #8f9db9; }
            QLineEdit#settingsPathInput { background: rgba(6, 8, 12, 0.8); border: 1px solid rgba(90, 142, 255, 0.4); border-radius: 8px; padding: 8px 10px; color: #f4f6ff; }
            QLineEdit#settingsPathInput:focus { border: 1px solid rgba(113, 201, 255, 0.8); background: rgba(12, 16, 22, 0.9); }
            QTabWidget::pane { border: none; background: #0f1720; }
            QTabBar::tab { background: #151b2b; color: #cfd7ff; border: 1px solid rgba(255, 255, 255, 0.08); border-bottom: none; border-top-left-radius: 10px; border-top-right-radius: 10px; padding: 6px 18px; margin-right: 6px; }
            QTabBar::tab:selected { background: #2b8bd5; color: #ffffff; border-color: #349ff3; }
            QTabBar::tab:hover { background: rgba(255, 255, 255, 0.08); }
        '''

        # Solid scroll thumbs and keyboard focus remain visible in either theme.
        for mode, handle, hover, accent in (
                ('light', '#c2ccda', '#94a4b9', '#2869d8'),
                ('dark', '#34445a', '#61748f', '#4c8dff')):
            detail_css = f'''
                QScrollBar::handle:vertical {{ background: {handle}; border-radius: 4px; }}
                QScrollBar::handle:vertical:hover {{ background: {hover}; }}
                QScrollBar::handle:vertical:pressed {{ background: {accent}; }}
                QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
                QScrollBar::handle:horizontal {{ background: {handle}; min-width: 28px; border-radius: 4px; }}
                QScrollBar::handle:horizontal:hover {{ background: {hover}; }}
                QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; height: 0; }}
                QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
                QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
                QAbstractSpinBox:focus, QComboBox:focus {{ border: 1px solid {accent}; }}
            '''
            if mode == 'light':
                light_css += detail_css
            else:
                dark_css += detail_css
        from aetherloom_core.ui.menus import stylesheet as menu_stylesheet
        return {'light': light_css + menu_stylesheet('light'),
                'dark': dark_css + menu_stylesheet('dark')}


    def _apply_theme(self, mode=None):
        mode = mode or getattr(self, '_theme_mode', 'dark')
        if mode not in getattr(self, '_themes', {}):
            mode = 'dark'
        self._theme_mode = mode
        if hasattr(self, '_decode_page'):
            self._decode_page.apply_theme()
        if hasattr(self, '_rh_dashboard'):
            self._rh_dashboard.apply_theme()
        from aetherloom_core.api_manager_ui import apply_theme as apply_api_theme
        apply_api_theme(self, mode)
        from aetherloom_core.ui.preferences import apply_settings_theme
        apply_settings_theme(self, mode)
        canvas = getattr(self, 'canvas_page', None)
        if canvas is not None:
            canvas.refresh_theme()
        css = self._themes.get(mode)
        if css:
            self.setStyleSheet(css)
            self._fit_sidebar_to_theme()
        from aetherloom_core.ui.menus import MenuTheme
        if not hasattr(self, '_menu_theme'):
            self._menu_theme = MenuTheme(self)
        self._menu_theme.refresh()
        if hasattr(self, 'local_page'):
            from aetherloom_core.local_browser_ui import stylesheet as local_browser_stylesheet
            self.local_page.setStyleSheet(local_browser_stylesheet(mode))
            for grid in (self.local_list_in, self.local_list_out):
                grid.viewport().update()
        local_preview = getattr(self, '_local_preview_window', None)
        if local_preview is not None:
            local_preview.apply_theme(mode)
        try:
            from aetherloom_core.rh_ui import app_stylesheet
            for page in list((getattr(self, '_rh_app_pages', {}) or {}).values()):
                if page is not None:
                    page.setStyleSheet(app_stylesheet(mode))
            refresh_navigation = getattr(self, '_rh_refresh_app_styles', None)
            if callable(refresh_navigation):
                refresh_navigation()
        except RuntimeError:
            # A page can have been removed immediately before a theme change.
            pass
        try:
            _set_native_titlebar_dark(self, mode == 'dark')
        except Exception:
            pass
        try:
            if hasattr(self, 'theme_toggle_btn') and self.theme_toggle_btn is not None:
                # icon-only; use tooltip to indicate action
                self.theme_toggle_btn.setToolTip('切换为日间模式' if mode == 'dark' else '切换为夜间模式')
                self._update_theme_toggle_icon(mode)
        except Exception:
            pass
        try:
            if getattr(self, '_compare_window', None) is not None:
                self._compare_window.sync_theme(mode)
        except Exception:
            pass
        home_page = getattr(self, 'home_page', None)
        if home_page is not None:
            home_page.set_theme(mode)
        # refresh any per-node preview placeholders so they match new theme
        try:
            try:
                labels = self.findChildren(QtWidgets.QLabel)
            except Exception:
                labels = []
            for lbl in (labels or []):
                try:
                    try:
                        is_prev = bool(lbl.property('rh_preview'))
                    except Exception:
                        is_prev = False
                    if not is_prev:
                        continue
                    # only refresh placeholder images (not labels showing a real file)
                    if getattr(lbl, '_last_path', None) is None:
                        try:
                            self._refresh_preview_placeholder(lbl)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass


    def _update_theme_toggle_icon(self, mode):
        try:
            if not hasattr(self, 'theme_toggle_btn') or self.theme_toggle_btn is None:
                return
            icon_dir = os.path.join(current_dir, 'icons')
            candidates = []
            if mode == 'dark':
                candidates.extend([
                    os.path.join(icon_dir, 'theme_toggle_dark.svg'),
                    os.path.join(icon_dir, 'theme_toggle_dark.jpeg'),
                    os.path.join(icon_dir, 'theme_toggle.svg'),
                    os.path.join(icon_dir, 'theme_toggle.jpeg'),
                ])
            else:
                candidates.extend([
                    os.path.join(icon_dir, 'theme_toggle_light.svg'),
                    os.path.join(icon_dir, 'theme_toggle_light.jpeg'),
                    os.path.join(icon_dir, 'theme_toggle.svg'),
                    os.path.join(icon_dir, 'theme_toggle.jpeg'),
                ])
            icon_obj = None
            for p in candidates:
                if os.path.exists(p):
                    icon_obj = QtGui.QIcon(p)
                    break
            if icon_obj:
                self.theme_toggle_btn.setIcon(icon_obj)
                # larger icon for visibility
                base_icon = getattr(self, '_theme_toggle_icon_px_base', 32)
                scale = getattr(self, '_ui_scale_factor', 1.0)
                icon_px = max(24, int(base_icon * scale))
                self.theme_toggle_btn.setIconSize(QtCore.QSize(icon_px, icon_px))
        except Exception:
            pass


    def _toggle_theme_mode(self):
        new_mode = 'light' if getattr(self, '_theme_mode', 'dark') == 'dark' else 'dark'
        self._apply_theme(new_mode)
        if isinstance(getattr(self, 'settings', None), dict):
            self.settings['theme_mode'] = new_mode
        try:
            self._save_settings()
        except Exception:
            pass


    def _refresh_preview_placeholder(self, lbl):
        """Render a theme-aware placeholder into a preview QLabel and clear any saved path."""
        try:
            if lbl is None:
                return
            try:
                target_w = max(120, (lbl.width() - 8) or 200)
            except Exception:
                target_w = 200
            target_h = 112 if lbl.property('rh_compact_input') else target_w
            ph = QtGui.QPixmap(target_w, target_h)
            bg = QtGui.QColor('#f0f0f0') if getattr(self, '_theme_mode', 'dark') == 'light' else QtGui.QColor('#121417')
            ph.fill(bg)
            painter = QtGui.QPainter(ph)
            try:
                pen = QtGui.QPen(QtGui.QColor(150, 150, 150, 80))
                pen.setStyle(QtCore.Qt.DashLine)
                painter.setPen(pen)
                painter.drawRect(4, 4, target_w-8, target_h-8)
                # draw a dim centered hint text
                try:
                    hint = '浏览或拖入本地文件'
                    # choose text color based on theme (semi-transparent)
                    if getattr(self, '_theme_mode', 'dark') == 'light':
                        tcol = QtGui.QColor(40, 40, 40, 140)
                    else:
                        tcol = QtGui.QColor(220, 220, 220, 120)
                    # smaller, capped point size so hint stays subtle
                    font_px = max(10, min(14, int(target_w * 0.035)))
                    f = QtGui.QFont()
                    f.setPointSize(font_px)
                    f.setBold(False)
                    painter.setFont(f)
                    painter.setPen(QtGui.QPen(tcol))
                    rect = QtCore.QRect(0, 0, target_w, target_h)
                    painter.drawText(rect, Qt.AlignCenter, hint)
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                try:
                    painter.end()
                except Exception:
                    pass
            lbl._orig_pixmap = ph
            try:
                scaled = ph.scaledToWidth(target_w, QtCore.Qt.SmoothTransformation)
            except Exception:
                scaled = ph
            try:
                lbl.setPixmap(scaled)
            except Exception:
                try:
                    lbl.clear()
                except Exception:
                    pass
            try:
                lbl.setFixedHeight(scaled.height() + 12)
            except Exception:
                pass
            # mark that this label currently shows a placeholder (no file path)
            try:
                lbl._last_path = None
            except Exception:
                pass
        except Exception:
            try:
                lbl.setText('')
            except Exception:
                pass


    def _show_toast(self, text, timeout=2000):
        """Show a transient bottom-right message using the selection info frame.
        Reuses `_selection_info_frame` if present; falls back to QMessageBox if not.
        """
        try:
            if getattr(self, '_selection_info_frame', None) is None or getattr(self, '_selection_info_label', None) is None:
                try:
                    QtWidgets.QMessageBox.information(self, '', text)
                except Exception:
                    pass
                return
            try:
                self._selection_info_label.setText(text)
            except Exception:
                try:
                    self._selection_info_label.setText(str(text))
                except Exception:
                    pass
            try:
                # size and move to bottom-right
                maxw = max(240, int(self.width() * 0.28))
                try:
                    self._selection_info_frame.setFixedWidth(min(maxw, 1000))
                except Exception:
                    pass
                self._selection_info_frame.adjustSize()
                fw = self._selection_info_frame.width()
                fh = self._selection_info_frame.height()
                margin = 16
                x = max(8, self.width() - fw - margin)
                y = max(8, self.height() - fh - margin)
                try:
                    self._selection_info_frame.move(x, y)
                    self._selection_info_frame.setVisible(True)
                except Exception:
                    pass
                # hide after timeout
                QtCore.QTimer.singleShot(int(timeout), lambda: (self._selection_info_frame.setVisible(False) if getattr(self, '_selection_info_frame', None) is not None else None))
            except Exception:
                pass
        except Exception:
            pass


    def _get_play_icon(self, size_px=None):
        """Return a crisp play icon, rendered from SVG with caching."""
        try:
            scale = getattr(self, '_ui_scale_factor', 1.0)
            base_px = getattr(self, '_play_icon_px_base', 100)
            if size_px is None:
                size_px = max(72, int(base_px * scale))
            cache = getattr(self, '_play_icon_cache', None)
            if cache is not None and size_px in cache:
                return cache[size_px]
            icon_obj = _svg_to_icon(PLAY_BUTTON_SVG, size_px)
            if (icon_obj is None or icon_obj.isNull()):
                icon_obj = self._fallback_play_icon(size_px)
            if cache is not None and icon_obj is not None and not icon_obj.isNull():
                cache[size_px] = icon_obj
            return icon_obj
        except Exception:
            return None


    def _fallback_play_icon(self, size_px):
        """Fallback painter-based play icon if QtSvg is unavailable."""
        try:
            pm = QtGui.QPixmap(size_px, size_px)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            rect = QtCore.QRectF(4, 4, size_px - 8, size_px - 8)
            grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QtGui.QColor(33, 125, 226))
            grad.setColorAt(1.0, QtGui.QColor(33, 199, 244))
            p.setBrush(QtGui.QBrush(grad))
            p.setPen(QtGui.QPen(QtGui.QColor(13, 59, 124), max(2.0, size_px * 0.035)))
            p.drawEllipse(rect)
            tri_margin = size_px * 0.26
            tri_width = size_px * 0.32
            triangle = QtGui.QPolygonF([
                QtCore.QPointF(tri_margin, size_px * 0.32),
                QtCore.QPointF(tri_margin, size_px * 0.68),
                QtCore.QPointF(tri_margin + tri_width, size_px * 0.5),
            ])
            p.setBrush(QtGui.QColor(255, 255, 255))
            p.setPen(QtGui.QPen(QtGui.QColor(13, 59, 124), max(2.0, size_px * 0.028)))
            p.drawPolygon(triangle)
            p.end()
            return QtGui.QIcon(pm)
        except Exception:
            return None


    def _refresh_output_play_icon(self, icon_px=None):
        """Ensure the overlay play button icon matches the current scale."""
        try:
            btn = getattr(self, 'output_play_btn', None)
            if btn is None:
                return
            if icon_px is None:
                icon_px = max(72, int(getattr(self, '_play_icon_px_base', 100) * getattr(self, '_ui_scale_factor', 1.0)))
            icon_obj = self._get_play_icon(icon_px)
            if icon_obj:
                btn.setIcon(icon_obj)
                btn.setIconSize(QtCore.QSize(icon_px, icon_px))
        except Exception:
            pass


    def _update_output_play_button_visibility(self, visible):
        """Show or hide the overlay play button on the output preview."""
        try:
            if hasattr(self, 'output_play_btn') and self.output_play_btn is not None:
                is_visible = bool(visible)
                self.output_play_btn.setVisible(is_visible)
                self.output_play_btn.setEnabled(is_visible)
                try:
                    if is_visible:
                        self.output_play_btn.raise_()
                except Exception:
                    pass
        except Exception:
            pass
