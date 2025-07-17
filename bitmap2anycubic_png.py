import sys
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QFileDialog,
    QHBoxLayout, QSpinBox, QMessageBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PIL import Image, ImageDraw, ImageOps
import os

class KreisProjektorGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("bitmap2anycubic")
        self.setMinimumSize(400, 200)

        self.image_path = None

        # Widgets
        self.label_info = QLabel("Wähle ein Bild aus, das links normal und rechts invertiert eingefügt wird.")
        self.btn_bild = QPushButton("Bild auswählen")
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(0, 254)
        self.spin_threshold.setValue(128)
        self.label_threshold = QLabel("Schwellwert (0-254):")
        self.btn_start = QPushButton("Verarbeiten und speichern")

        # Layout
        layout = QVBoxLayout()
        layout.addWidget(self.label_info)
        layout.addWidget(self.btn_bild)

        threshold_layout = QHBoxLayout()
        threshold_layout.addWidget(self.label_threshold)
        threshold_layout.addWidget(self.spin_threshold)
        layout.addLayout(threshold_layout)

        layout.addWidget(self.btn_start)
        self.setLayout(layout)

        # Verbindungen
        self.btn_bild.clicked.connect(self.bild_auswaehlen)
        self.btn_start.clicked.connect(self.verarbeiten)

    def bild_auswaehlen(self):
        path, _ = QFileDialog.getOpenFileName(self, "Bild öffnen", "", "Bilder (*.png *.jpg *.jpeg *.gif *.bmp)")
        if path:
            self.image_path = path
            self.label_info.setText(f"Ausgewählt: {os.path.basename(path)}")

    def verarbeiten(self):
        if not self.image_path:
            QMessageBox.warning(self, "Fehler", "Bitte wähle zuerst ein Bild aus.")
            return

        threshold = self.spin_threshold.value()

        output_path, _ = QFileDialog.getSaveFileName(self, "Speichern unter", "output.png", "PNG Dateien (*.png)")
        if not output_path:
            return

        try:
            self.erzeuge_bild(self.image_path, output_path, threshold)
            QMessageBox.information(self, "Fertig", f"Bild gespeichert unter:\n{output_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", str(e))

    def erzeuge_bild(self, input_path, output_path, threshold):
        width_px, height_px = 13312, 5120
        screen_width_mm = 223.642
        screen_height_mm = 126.48

        px_per_mm_x = width_px / screen_width_mm
        px_per_mm_y = height_px / screen_height_mm

        radius_px_x = int((100 / 2) * px_per_mm_x)
        radius_px_y = int((100 / 2) * px_per_mm_y)

        center_y = height_px // 2
        left_center_x = int(60 * px_per_mm_x)
        right_center_x = int((screen_width_mm - 60) * px_per_mm_x)

        base = Image.new("L", (width_px, height_px), 0)
        img = Image.open(input_path)

        self.place_in_circle(base, img, (left_center_x, center_y), radius_px_x, radius_px_y, threshold, invert=False)
        self.place_in_circle(base, img, (right_center_x, center_y), radius_px_x, radius_px_y, threshold, invert=True)
        base = ImageOps.mirror(base) 

        base.save(output_path)

    def place_in_circle(self, base_img, content_img, center, radius_px_x, radius_px_y, threshold=None, invert=False):
        content_img = content_img.convert("L")

        if threshold is not None:
            content_img = content_img.point(lambda x: 255 if x >= threshold else 0, mode='L')

        if invert:
            content_img = ImageOps.invert(content_img)

        min_side = min(content_img.size)
        content_cropped = content_img.crop( (
            (content_img.width - min_side) // 2,
            (content_img.height - min_side) // 2,
            (content_img.width + min_side) // 2,
            (content_img.height + min_side) // 2
        ))

        scaled = content_cropped.resize((2 * radius_px_x, 2 * radius_px_y), resample=Image.Resampling.LANCZOS)

        mask = Image.new("L", (2 * radius_px_x, 2 * radius_px_y), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse([0, 0, 2 * radius_px_x, 2 * radius_px_y], fill=255)

        paste_position = (center[0] - radius_px_x, center[1] - radius_px_y)
        base_img.paste(scaled, paste_position, mask)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = KreisProjektorGUI()
    window.show()
    sys.exit(app.exec_())