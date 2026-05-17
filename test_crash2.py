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
        # self.w.finished.connect(self.w.deleteLater) # NO deleteLater
        self.w.finished.connect(self.on_finished)
        
        # self.t.finished.connect(self.t.deleteLater) # NO deleteLater
        self.t.finished.connect(self.on_t_finished)
        
        self.t.start()
        
    def on_finished(self):
        self.w = None
        
    def on_t_finished(self):
        self.t = None

app = QApplication([])
tester = Tester()
QTimer.singleShot(1000, app.quit)
app.exec()
print("Done")