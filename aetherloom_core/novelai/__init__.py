"""NovelAI illustration workspace; networking and editor state stay independent."""


def install_page(owner):
    from PyQt5 import QtCore, QtWidgets
    from .page import NovelAIPage
    from .service import can_close_service, shutdown_service

    class LazyPage(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(owner)
            self.workspace = None
            self.box = QtWidgets.QVBoxLayout(self)
            self.box.setContentsMargins(0, 0, 0, 0)

        def _ensure_workspace(self):
            if self.workspace is None:
                self.workspace = NovelAIPage(owner, self)
                self.box.addWidget(self.workspace)
                # Native Qt polishes inherited shell styles during Show. Refresh
                # once after that pass so this lazily built page keeps its scope.
                self._theme_ready = QtCore.QTimer(self)
                self._theme_ready.setSingleShot(True)
                self._theme_ready.timeout.connect(self.workspace.apply_theme)
                self._theme_ready.start(0)
            return self.workspace

        def showEvent(self, event):
            super().showEvent(event)
            self._ensure_workspace()

        def apply_theme(self):
            if self.workspace is not None:
                self.workspace.apply_theme()

        def can_close(self):
            if self.workspace is not None:
                return self.workspace.can_close()
            if can_close_service(owner, self):
                return True
            self._ensure_workspace().show_queue()
            return False

        def shutdown(self):
            if self.workspace is not None:
                self.workspace.shutdown()
            shutdown_service(owner)

    owner.novelai_page = LazyPage()
    owner.pages.addWidget(owner.novelai_page)

    def select():
        for button in owner._sidebar_buttons:
            button.setChecked(button is owner.novelai_btn)
        owner.pages.setCurrentWidget(owner.novelai_page)

    owner.novelai_btn.clicked.connect(select)
    owner.pages.currentChanged.connect(
        lambda unused: owner.novelai_btn.setChecked(owner.pages.currentWidget() is owner.novelai_page))
