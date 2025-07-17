import sys
import gdspy
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QFileDialog,
    QComboBox, QMessageBox
)
from PIL import Image, ImageDraw, ImageOps
import os

class GDSKreisProjektorGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GDS-Kreisprojektor")
        self.setMinimumSize(500, 300)
        self.library = None

        self.label_info = QLabel("GDS-Datei laden")
        self.btn_gds = QPushButton("GDS-Datei auswählen")
        self.combo_cell = QComboBox()
        self.combo_layer = QComboBox()
        self.btn_start = QPushButton("Rendern und speichern")

        layout = QVBoxLayout()
        layout.addWidget(self.label_info)
        layout.addWidget(self.btn_gds)
        layout.addWidget(QLabel("Zelle wählen:")); layout.addWidget(self.combo_cell)
        layout.addWidget(QLabel("Layer wählen:")); layout.addWidget(self.combo_layer)
        layout.addWidget(self.btn_start)
        self.setLayout(layout)

        self.btn_gds.clicked.connect(self.gds_laden)
        self.btn_start.clicked.connect(self.verarbeiten)

    def gds_laden(self):
        path, _ = QFileDialog.getOpenFileName(self, "GDS-Datei öffnen", "", "GDSII-Dateien (*.gds *.gds2)")
        if not path:
            return
        self.library = gdspy.GdsLibrary(infile=path)
        self.label_info.setText(f"Geladen: {os.path.basename(path)}")
        self.combo_cell.clear(); self.combo_layer.clear()

        for name in self.library.cells:
            self.combo_cell.addItem(name)
        layers = set()
        for cell in self.library.cells.values():
            for (ly, dt), poly_list in cell.get_polygons(by_spec=True).items():
                layers.add(ly)
        for ly in sorted(layers):
            self.combo_layer.addItem(str(ly))

    def verarbeiten(self):
        if not self.library:
            QMessageBox.warning(self, "Fehler", "Bitte GDS-Datei laden.")
            return

        original_cell = self.library.cells[self.combo_cell.currentText()]
        layer = int(self.combo_layer.currentText())

        # Kopie der Zelle erstellen und Instanzen auflösen
        flat_cell = original_cell.copy(name="__temp_flat__")
        flat_cell.flatten()

        polys = flat_cell.get_polygons(by_spec=True).get((layer, 0), [])
        if not polys:
            QMessageBox.critical(self, "Fehler", f"Keine Geometrie auf Layer {layer}")
            return

        # Dateidialog ohne native Dialoge (macOS Crash-Fix)
        dialog = QFileDialog(self, "Speichern unter", "output.png", "PNG-Datei (*.png)")
        dialog.setAcceptMode(QFileDialog.AcceptSave)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        if not dialog.exec_():
            return
        output_path = dialog.selectedFiles()[0]

        # Display-Auflösung und -Maße
        W_px, H_px = 13312, 5120
        screen_width_mm, screen_height_mm = 223.642, 126.48
        px_per_mm_x = W_px / screen_width_mm
        px_per_mm_y = H_px / screen_height_mm

        # Zielkreis: 100 mm Durchmesser → 50 mm Radius
        radius_px_x = int(50 * px_per_mm_x)
        radius_px_y = int(50 * px_per_mm_y)

        # Skalierung: µm → mm → Pixel
        scale_um_to_px_x = px_per_mm_x / 1000
        scale_um_to_px_y = px_per_mm_y / 1000

        # Bounding-Box ermitteln, um zentrieren zu können
        bbox = flat_cell.get_bounding_box()
        cx_um = (bbox[0][0] + bbox[1][0]) / 2
        cy_um = (bbox[0][1] + bbox[1][1]) / 2

        # Graustufen-Bild vorbereiten (nur Kreisgröße)
        gds_img = Image.new("L", (2 * radius_px_x, 2 * radius_px_y), 0)
        draw = ImageDraw.Draw(gds_img)

        for poly in polys:
            pts = []
            for x, y in poly:
                xs = (x - cx_um) * scale_um_to_px_x + radius_px_x
                ys = (cy_um - y) * scale_um_to_px_y + radius_px_y
                pts.append((xs, ys))
            draw.polygon(pts, fill=255)

        # Grundbild vorbereiten
        base = Image.new("L", (W_px, H_px), 0)
        self.paste_circle(base, gds_img, radius_px_x, radius_px_y, px_per_mm_x, px_per_mm_y, pos="left", invert=False)
        self.paste_circle(base, gds_img, radius_px_x, radius_px_y, px_per_mm_x, px_per_mm_y, pos="right", invert=True)
        base = ImageOps.mirror(base)                         

        try:
            base.save(output_path)
            QMessageBox.information(self, "Fertig", f"Gespeichert: {output_path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler beim Speichern", str(e))

        # Aufräumen der temporären Zelle
        if "__temp_flat__" in self.library.cells:
            del self.library.cells["__temp_flat__"]

    def paste_circle(self, base, img, rx, ry, px_mm_x, px_mm_y, pos, invert=False):
        if invert:
            img = ImageOps.invert(img)
        cy = base.height // 2
        cx = int(60 * px_mm_x) if pos == "left" else int((base.width / px_mm_x - 60) * px_mm_x)
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, *img.size], fill=255)
        base.paste(img, (cx - rx, cy - ry), mask)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    gui = GDSKreisProjektorGUI()
    gui.show()
    sys.exit(app.exec_())