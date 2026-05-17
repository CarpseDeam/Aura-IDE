import sys
from PySide6.QtCore import QObject, QThread, Signal, QTimer
from PySide6.QtWidgets import QApplication

class Worker(QObject):
    finished = Signal()
    def run(self):
        self.finished.emit()

class Tester(QObject):
    def __init__(self):
        super().__init__()
        self.t = QThread(self)
        self.w = Worker()
        self.w.moveToThread(self.t)
        self.t.started.connect(self.w.run)
        
        self.w.finished.connect(self.t.quit)
        self.w.finished.connect(self.w.deleteLater)
        self.w.finished.connect(self.on_finished)
        
        self.t.finished.connect(self.t.deleteLater)
        self.t.finished.connect(self.on_t_finished)
        
        self.t.start()
        
    def on_finished(self):
        # Do not delete the Python reference yet! Let deleteLater handle C++ deletion,
        # but actually if we don't delete Python reference, Python will delete it when Tester dies.
        pass
        
    def on_t_finished(self):
        pass

app = QApplication([])
tester = Tester()
QTimer.singleShot(1000, app.quit)
app.exec()
print("Done")