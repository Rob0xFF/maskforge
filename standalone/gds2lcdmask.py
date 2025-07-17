import sys
import os
import io
import math
import uuid  # <-- für eindeutige Temp-CelNamen

import gdspy
from PIL import Image, ImageDraw, ImageOps

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QFileDialog,
    QVBoxLayout, QWidget, QHBoxLayout, QSpinBox, QDoubleSpinBox,
    QLineEdit, QGroupBox, QGridLayout, QSizePolicy, QFrame, QGraphicsView,
    QGraphicsScene, QComboBox
)
from PyQt5.QtCore import (
    Qt, QCoreApplication, QSettings, QObject, QThread, pyqtSignal
)
from PyQt5.QtGui import QPixmap, QPainter, QColor


# ------------------------------------------------------------------
# App identity / settings
# ------------------------------------------------------------------
COMPANY_NAME = "Rob0xFF"
APP_NAME_SETTINGS = "GDS LCD Photomask"   # ASCII-safe name for QSettings
WINDOW_TITLE_DISPLAY = "GDS \u2192 LCD Photomask"


# ------------------------------------------------------------------
# Default values (overridden via UI or QSettings load)
# ------------------------------------------------------------------
DISPLAY_PIX_W = 13312
DISPLAY_PIX_H = 5120
DISPLAY_W_MM   = 223.642
DISPLAY_H_MM   = 126.48

# Kreis- & Platzierungs-Parameter aus deinem Originaltool
CIRCLE_DIAM_MM   = 100.0   # -> Radius 50 mm
CIRCLE_OFFSET_MM = 60.0    # Abstand Zentrum vom linken/rechten Rand

# Derived (updated in recompute_scalars)
PX_PER_MM_X = None
PX_PER_MM_Y = None


def recompute_scalars():
    """Compute derived scale factors."""
    global PX_PER_MM_X, PX_PER_MM_Y
    PX_PER_MM_X = DISPLAY_PIX_W / DISPLAY_W_MM
    PX_PER_MM_Y = DISPLAY_PIX_H / DISPLAY_H_MM


# initial compute
recompute_scalars()


# ------------------------------------------------------------------
# Core GDS rendering — funktional unverändert; nur Temp-Cell jetzt eindeutig.
# ------------------------------------------------------------------
def render_gds_to_photomask(
    gds_path: str,
    cell_name: str,
    layer: int,
    W_px: int,
    H_px: int,
    screen_width_mm: float,
    screen_height_mm: float,
    circle_diam_mm: float = CIRCLE_DIAM_MM,
    circle_offset_mm: float = CIRCLE_OFFSET_MM,
):
    """
    Rendert ausgewählte Cell + Layer einer GDS in das LCD-Format mit
    zwei Kreisprojektionen (links normal, rechts invertiert, dann Mirror).
    Gibt ein PIL Image (L) zurück.
    """

    # GDS laden
    lib = gdspy.GdsLibrary(infile=gds_path)
    if cell_name not in lib.cells:
        raise ValueError(f"Cell '{cell_name}' not found in GDS.")

    original_cell = lib.cells[cell_name]

    # Eindeutiger temporärer Name, um Kollisionen zu vermeiden
    temp_name = f"__temp_flat__{uuid.uuid4().hex}"

    # Kopie + Flatten
    flat_cell = original_cell.copy(name=temp_name)
    flat_cell.flatten()

    # Polygone aus Layer (DataType 0 wie in deinem Code)
    polys = flat_cell.get_polygons(by_spec=True).get((layer, 0), [])
    if not polys:
        # Aufräumen vor Exception
        if temp_name in lib.cells:
            del lib.cells[temp_name]
        raise ValueError(f"No geometry on layer {layer}.")

    # Display-Skalierung
    px_per_mm_x = W_px / screen_width_mm
    px_per_mm_y = H_px / screen_height_mm

    # Kreis-Radien
    radius_mm = circle_diam_mm / 2.0
    radius_px_x = int(radius_mm * px_per_mm_x)
    radius_px_y = int(radius_mm * px_per_mm_y)

    # µm → mm → px
    scale_um_to_px_x = px_per_mm_x / 1000.0
    scale_um_to_px_y = px_per_mm_y / 1000.0

    # Bounding-Box → Zentrierung
    bbox = flat_cell.get_bounding_box()
    if bbox is None:
        if temp_name in lib.cells:
            del lib.cells[temp_name]
        raise ValueError("Empty cell; no bounding box found.")

    cx_um = (bbox[0][0] + bbox[1][0]) / 2.0
    cy_um = (bbox[0][1] + bbox[1][1]) / 2.0

    # Kreisgröße
    gds_img = Image.new("L", (2 * radius_px_x, 2 * radius_px_y), 0)
    draw = ImageDraw.Draw(gds_img)

    # Polygone zeichnen
    for poly in polys:
        pts = []
        for x, y in poly:
            xs = (x - cx_um) * scale_um_to_px_x + radius_px_x
            ys = (cy_um - y) * scale_um_to_px_y + radius_px_y
            pts.append((xs, ys))
        draw.polygon(pts, fill=255)

    # Grundbild LCD
    base = Image.new("L", (W_px, H_px), 0)

    # linke Kreisprojektion (nicht invertiert)
    paste_circle(
        base=base,
        img=gds_img,
        rx=radius_px_x,
        ry=radius_px_y,
        px_mm_x=px_per_mm_x,
        px_mm_y=px_per_mm_y,
        pos="left",
        offset_mm=circle_offset_mm,
        invert=False,
    )

    # rechte Kreisprojektion (invertiert)
    paste_circle(
        base=base,
        img=gds_img,
        rx=radius_px_x,
        ry=radius_px_y,
        px_mm_x=px_per_mm_x,
        px_mm_y=px_per_mm_y,
        pos="right",
        offset_mm=circle_offset_mm,
        invert=True,
    )

    # abschließend spiegeln (wie Original)
    base = ImageOps.mirror(base)

    # Aufräumen temporärer Zelle
    if temp_name in lib.cells:
        del lib.cells[temp_name]

    return base


def paste_circle(base, img, rx, ry, px_mm_x, px_mm_y, pos, offset_mm, invert=False):
    """Unveränderte Kreisprojektion aus deinem Original (parametrisiert)."""
    if invert:
        img = ImageOps.invert(img)

    cy = base.height // 2
    if pos == "left":
        cx = int(offset_mm * px_mm_x)
    else:  # right
        # (Display_mm_width - offset_mm) * px_per_mm  (entspricht deinem Original)
        disp_mm_w = base.width / px_mm_x
        cx = int((disp_mm_w - offset_mm) * px_mm_x)

    # Kreis-Maske (Ellipse)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, *img.size], fill=255)

    base.paste(img, (cx - rx, cy - ry), mask)


# ------------------------------------------------------------------
# Worker thread: schwere Renderarbeit offloaden
# ------------------------------------------------------------------
class PrepareWorker(QObject):
    finished = pyqtSignal(Image.Image)  # final photomask image
    error = pyqtSignal(str)

    def __init__(self, gds_path: str, cell_name: str, layer: int,
                 W_px: int, H_px: int,
                 screen_w_mm: float, screen_h_mm: float,
                 circle_diam_mm: float, circle_offset_mm: float):
        super().__init__()
        self.gds_path = gds_path
        self.cell_name = cell_name
        self.layer = layer
        self.W_px = W_px
        self.H_px = H_px
        self.screen_w_mm = screen_w_mm
        self.screen_h_mm = screen_h_mm
        self.circle_diam_mm = circle_diam_mm
        self.circle_offset_mm = circle_offset_mm

    def run(self):
        try:
            img = render_gds_to_photomask(
                gds_path=self.gds_path,
                cell_name=self.cell_name,
                layer=self.layer,
                W_px=self.W_px,
                H_px=self.H_px,
                screen_width_mm=self.screen_w_mm,
                screen_height_mm=self.screen_h_mm,
                circle_diam_mm=self.circle_diam_mm,
                circle_offset_mm=self.circle_offset_mm,
            )
        except Exception as e:
            self.error.emit(str(e))
            return

        self.finished.emit(img)


# ------------------------------------------------------------------
# Preview View (zoom/pan, harte Pixel)
# ------------------------------------------------------------------
class PreviewView(QGraphicsView):
    """
    Mouse-wheel zoomable preview.
    Hard pixels: SmoothPixmapTransform disabled.
    """
    def __init__(self, width: int, height: int, parent=None):
        super().__init__(parent)
        self._w = width
        self._h = height
        self.setFixedSize(self._w, self._h)
        self.setFrameStyle(QFrame.Box | QFrame.Plain)
        self.setLineWidth(1)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self._pixmap_item = None
        self._placeholder_pixmap = self._make_placeholder_pixmap()
        self._set_pixmap(self._placeholder_pixmap, fit=False)

        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setInteractive(True)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)

        self.setRenderHint(QPainter.SmoothPixmapTransform, False)
        self.setRenderHint(QPainter.Antialiasing, False)

        # Zoom limits (aus deiner Anweisung: _min_scale = 0.05, _max_scale = 10.0)
        self._min_scale = 0.05
        self._max_scale = 10.0

    def _make_placeholder_pixmap(self) -> QPixmap:
        pix = QPixmap(self._w, self._h)
        pix.fill(QColor("#303030"))
        painter = QPainter(pix)
        painter.setPen(QColor("#b0b0b0"))
        font = painter.font()
        font.setPointSize(24)
        painter.setFont(font)
        painter.drawText(pix.rect(), Qt.AlignCenter, "Preview")
        painter.end()
        return pix

    def reset_placeholder(self):
        self._set_pixmap(self._placeholder_pixmap, fit=False)

    def set_image_pixmap(self, pixmap: QPixmap):
        self._set_pixmap(pixmap, fit=True)

    def _set_pixmap(self, pixmap: QPixmap, fit: bool):
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self.resetTransform()
        if fit:
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        if self._pixmap_item is None:
            super().wheelEvent(event)
            return
        old_pos = self.mapToScene(event.pos())
        angle = event.angleDelta().y()
        if angle == 0:
            return
        factor = 1.25 if angle > 0 else 0.8
        cur_scale = self.transform().m11()
        new_scale = cur_scale * factor
        if new_scale < self._min_scale:
            factor = self._min_scale / cur_scale
        elif new_scale > self._max_scale:
            factor = self._max_scale / cur_scale
        self.scale(factor, factor)
        new_pos = self.mapToScene(event.pos())
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    def mouseDoubleClickEvent(self, event):
        if self._pixmap_item is not None:
            self.resetTransform()
            self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
        super().mouseDoubleClickEvent(event)


# ------------------------------------------------------------------
# Main GUI
# ------------------------------------------------------------------
class MainGUI(QMainWindow):
    FIELD_WIDTH = 150
    PREVIEW_W = 800
    PREVIEW_H = 600

    COLOR_OK = "#4caf50"
    COLOR_ERR = "#f44336"
    COLOR_BUSY = "#2196f3"

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE_DISPLAY)

        # QSettings
        self.settings = QSettings(COMPANY_NAME, APP_NAME_SETTINGS)

        # runtime state
        self.image = None
        self.gds_path = None
        self.selected_cell = None
        self.selected_layer = None

        # thread refs
        self._prepare_thread = None
        self._prepare_worker = None

        # ------------------------------------------------------------------
        # Files group
        # ------------------------------------------------------------------
        self.gds_edit = QLineEdit()
        self.gds_edit.setPlaceholderText("Select GDS file…")
        self.gds_browse_btn = QPushButton("Browse...")
        self.gds_browse_btn.clicked.connect(self.browse_gds)

        self.png_edit = QLineEdit()
        self.png_edit.setPlaceholderText("Select output PNG…")
        self.png_browse_btn = QPushButton("Browse...")
        self.png_browse_btn.clicked.connect(self.browse_png)

        files_grid = QGridLayout()
        lbl_gds = QLabel("GDS:")
        lbl_gds.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_png = QLabel("Output PNG:")
        lbl_png.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        files_grid.addWidget(lbl_gds, 0, 0)
        files_grid.addWidget(self.gds_edit, 0, 1)
        files_grid.addWidget(self.gds_browse_btn, 0, 2)

        files_grid.addWidget(lbl_png, 1, 0)
        files_grid.addWidget(self.png_edit, 1, 1)
        files_grid.addWidget(self.png_browse_btn, 1, 2)

        files_grid.setColumnStretch(0, 0)
        files_grid.setColumnStretch(1, 1)
        files_grid.setColumnStretch(2, 0)

        files_group = QGroupBox("Files")
        files_group.setLayout(files_grid)

        # ------------------------------------------------------------------
        # Settings group (Display)
        # ------------------------------------------------------------------
        self.sb_disp_px_w = QSpinBox(); self.sb_disp_px_w.setRange(1, 200000); self.sb_disp_px_w.setAlignment(Qt.AlignRight); self.sb_disp_px_w.setFixedWidth(self.FIELD_WIDTH)
        self.sb_disp_px_h = QSpinBox(); self.sb_disp_px_h.setRange(1, 200000); self.sb_disp_px_h.setAlignment(Qt.AlignRight); self.sb_disp_px_h.setFixedWidth(self.FIELD_WIDTH)
        self.sb_disp_w_mm = QDoubleSpinBox(); self.sb_disp_w_mm.setDecimals(3); self.sb_disp_w_mm.setRange(0.001, 10000); self.sb_disp_w_mm.setAlignment(Qt.AlignRight); self.sb_disp_w_mm.setFixedWidth(self.FIELD_WIDTH)
        self.sb_disp_h_mm = QDoubleSpinBox(); self.sb_disp_h_mm.setDecimals(3); self.sb_disp_h_mm.setRange(0.001, 10000); self.sb_disp_h_mm.setAlignment(Qt.AlignRight); self.sb_disp_h_mm.setFixedWidth(self.FIELD_WIDTH)

        settings_grid = QGridLayout()
        row = 0
        settings_grid.addWidget(self._mk_label("Display Width (px)"),  row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_px_w,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("Display Height (px)"), row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_px_h,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("Display Width (mm)"),  row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_w_mm,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("Display Height (mm)"), row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_h_mm,  row, 2, Qt.AlignRight); row += 1

        settings_grid.setColumnStretch(0, 0)
        settings_grid.setColumnStretch(1, 1)
        settings_grid.setColumnStretch(2, 0)

        settings_vlayout = QVBoxLayout()
        settings_vlayout.addLayout(settings_grid)

        settings_group = QGroupBox("Settings")
        settings_group.setLayout(settings_vlayout)

        # ------------------------------------------------------------------
        # Design Selection group (Cell & Layer)
        # ------------------------------------------------------------------
        self.combo_cell = QComboBox()
        self.combo_layer = QComboBox()
        self.combo_cell.setEnabled(False)
        self.combo_layer.setEnabled(False)

        design_grid = QGridLayout()
        design_grid.addWidget(self._mk_label("Cell"),   0, 0, Qt.AlignLeft)
        design_grid.addWidget(self.combo_cell,          0, 2)
        design_grid.addWidget(self._mk_label("Layer"),  1, 0, Qt.AlignLeft)
        design_grid.addWidget(self.combo_layer,         1, 2)

        design_grid.setColumnStretch(0, 0)
        design_grid.setColumnStretch(1, 1)
        design_grid.setColumnStretch(2, 0)

        design_group = QGroupBox("Design Selection")
        design_group.setLayout(design_grid)

        # ------------------------------------------------------------------
        # Action buttons
        # ------------------------------------------------------------------
        self.prepare_btn = QPushButton("Prepare Output")
        self.save_btn = QPushButton("Save PNG"); self.save_btn.setEnabled(False)
        self.prepare_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.save_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prepare_btn.clicked.connect(self.prepare_output)
        self.save_btn.clicked.connect(self.save_png)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.prepare_btn)
        buttons_row.addWidget(self.save_btn)

        # ------------------------------------------------------------------
        # Status row with colored indicator (spacing 4px)
        # ------------------------------------------------------------------
        self.ind = QLabel()
        self.ind.setFixedSize(10, 10)
        self.ind.setStyleSheet("border-radius:5px;background:#4caf50;")  # init green

        self.status_label = QLabel("")

        status_row = QHBoxLayout()
        status_row.addWidget(self.ind)
        status_row.addSpacing(4)
        status_row.addWidget(self.status_label, 1)
        status_row.addStretch(0)

        # ------------------------------------------------------------------
        # Left control panel
        # ------------------------------------------------------------------
        controls_layout = QVBoxLayout()
        controls_layout.addWidget(files_group)
        controls_layout.addWidget(settings_group)
        controls_layout.addWidget(design_group)
        controls_layout.addLayout(buttons_row)
        controls_layout.addLayout(status_row)
        controls_layout.addStretch(1)
        controls_widget = QWidget(); controls_widget.setLayout(controls_layout)

        # ------------------------------------------------------------------
        # Preview panel (right column)
        # ------------------------------------------------------------------
        self.preview_view = PreviewView(800, 600)
        preview_layout = QVBoxLayout()
        preview_layout.addWidget(self.preview_view, alignment=Qt.AlignTop | Qt.AlignLeft)
        preview_layout.addStretch(1)
        preview_widget = QWidget(); preview_widget.setLayout(preview_layout)

        # ------------------------------------------------------------------
        # Main 2-column layout
        # ------------------------------------------------------------------
        main_hlayout = QHBoxLayout()
        main_hlayout.addWidget(controls_widget)
        main_hlayout.addWidget(preview_widget)
        container = QWidget(); container.setLayout(main_hlayout)
        self.setCentralWidget(container)

        # load settings and init status
        self.load_settings()
        self._set_status("Ready.", "ok")

        # respond to combo changes (remember selection for settings)
        self.combo_cell.currentIndexChanged.connect(self._cell_changed)
        self.combo_layer.currentIndexChanged.connect(self._layer_changed)

    # ------------------------------------------------------------------
    # Status helper
    # ------------------------------------------------------------------
    def _set_status(self, text: str, state: str):
        color = self.COLOR_OK
        if state in ("busy", "preparing"):
            color = self.COLOR_BUSY
        elif state in ("error", "err", "fail"):
            color = self.COLOR_ERR
        self.ind.setStyleSheet(f"border-radius:5px;background:{color};")
        self.status_label.setText(text)

    # helper for consistent labels
    def _mk_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return lbl

    # enable/disable all user input controls during busy work
    def _set_controls_enabled(self, enabled: bool):
        self.gds_edit.setEnabled(enabled)
        self.gds_browse_btn.setEnabled(enabled)
        self.png_edit.setEnabled(enabled)
        self.png_browse_btn.setEnabled(enabled)
        self.sb_disp_px_w.setEnabled(enabled)
        self.sb_disp_px_h.setEnabled(enabled)
        self.sb_disp_w_mm.setEnabled(enabled)
        self.sb_disp_h_mm.setEnabled(enabled)
        self.combo_cell.setEnabled(enabled and self.combo_cell.count() > 0)
        self.combo_layer.setEnabled(enabled and self.combo_layer.count() > 0)
        self.prepare_btn.setEnabled(enabled)
        # save_btn separat

    # ------------------------------------------------------------------
    # QSettings load/save
    # ------------------------------------------------------------------
    def load_settings(self):
        gds_path = self.settings.value("paths/gds", "", type=str)
        png_path = self.settings.value("paths/output_png", "", type=str)
        self.gds_edit.setText(gds_path)
        self.png_edit.setText(png_path)

        px_w = self.settings.value("display/pix_w", DISPLAY_PIX_W, type=int)
        px_h = self.settings.value("display/pix_h", DISPLAY_PIX_H, type=int)
        d_w  = self.settings.value("display/mm_w",  DISPLAY_W_MM, type=float)
        d_h  = self.settings.value("display/mm_h",  DISPLAY_H_MM, type=float)

        self.sb_disp_px_w.setValue(px_w)
        self.sb_disp_px_h.setValue(px_h)
        self.sb_disp_w_mm.setValue(d_w)
        self.sb_disp_h_mm.setValue(d_h)

        cell_last  = self.settings.value("gds/cell",  "", type=str)
        layer_last = self.settings.value("gds/layer", "", type=str)

        if gds_path and os.path.isfile(gds_path):
            self._load_gds_metadata(gds_path, cell_last, layer_last)

    def save_settings(self):
        self.settings.setValue("paths/gds",        self.gds_edit.text().strip())
        self.settings.setValue("paths/output_png", self.png_edit.text().strip())
        self.settings.setValue("display/pix_w", self.sb_disp_px_w.value())
        self.settings.setValue("display/pix_h", self.sb_disp_px_h.value())
        self.settings.setValue("display/mm_w",  self.sb_disp_w_mm.value())
        self.settings.setValue("display/mm_h",  self.sb_disp_h_mm.value())
        self.settings.setValue("gds/cell",  self.selected_cell if self.selected_cell else "")
        self.settings.setValue("gds/layer", self.selected_layer if self.selected_layer is not None else "")
        self.settings.sync()

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # GDS load (UI action)
    # ------------------------------------------------------------------
    def browse_gds(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Select GDS file", "", "GDSII (*.gds *.gds2)"
        )
        if fn:
            self.gds_edit.setText(fn)
            self.gds_path = fn
            self._load_gds_metadata(fn)
            self._set_status("GDS file loaded (not processed yet).", "ok")
            self.save_settings()

    def _load_gds_metadata(self, path: str, prefer_cell: str = "", prefer_layer: str = ""):
        """Parse GDS to populate cell & layer combo boxes. Blocking parse (UI freeze risk if huge)."""
        try:
            lib = gdspy.GdsLibrary(infile=path)
        except Exception as e:
            self._set_status(f"Failed to load GDS: {e}", "error")
            self.combo_cell.clear(); self.combo_layer.clear()
            self.combo_cell.setEnabled(False); self.combo_layer.setEnabled(False)
            return

        self.combo_cell.blockSignals(True)
        self.combo_layer.blockSignals(True)
        self.combo_cell.clear()
        self.combo_layer.clear()

        # cells
        names = list(lib.cells.keys())
        for nm in names:
            self.combo_cell.addItem(nm)

        # gather layers
        layers = set()
        for cell in lib.cells.values():
            for (ly, dt), poly_list in cell.get_polygons(by_spec=True).items():
                layers.add(ly)
        for ly in sorted(layers):
            self.combo_layer.addItem(str(ly))

        self.combo_cell.blockSignals(False)
        self.combo_layer.blockSignals(False)

        self.combo_cell.setEnabled(self.combo_cell.count() > 0)
        self.combo_layer.setEnabled(self.combo_layer.count() > 0)

        self.gds_path = path

        # restore preferred selections if available
        if prefer_cell and prefer_cell in names:
            idx = self.combo_cell.findText(prefer_cell)
            if idx >= 0:
                self.combo_cell.setCurrentIndex(idx)
        if prefer_layer:
            idx = self.combo_layer.findText(str(prefer_layer))
            if idx >= 0:
                self.combo_layer.setCurrentIndex(idx)

        # snapshot currently selected
        self.selected_cell = self.combo_cell.currentText() if self.combo_cell.count() else None
        try:
            self.selected_layer = int(self.combo_layer.currentText()) if self.combo_layer.count() else None
        except ValueError:
            self.selected_layer = None

    def _cell_changed(self, idx: int):
        self.selected_cell = self.combo_cell.currentText()
        self.save_settings()

    def _layer_changed(self, idx: int):
        try:
            self.selected_layer = int(self.combo_layer.currentText())
        except ValueError:
            self.selected_layer = None
        self.save_settings()

    # ------------------------------------------------------------------
    # PNG output path
    # ------------------------------------------------------------------
    def browse_png(self):
        fn, _ = QFileDialog.getSaveFileName(
            self, "Select output PNG", "out.png", "PNG (*.png)"
        )
        if fn:
            if not fn.lower().endswith(".png"):
                fn += ".png"
            self.png_edit.setText(fn)
            self._set_status("Output path set.", "ok")
            self.save_settings()

    # ------------------------------------------------------------------
    # Apply settings from UI → globals
    # ------------------------------------------------------------------
    def apply_user_values(self, silent: bool = False) -> bool:
        global DISPLAY_PIX_W, DISPLAY_PIX_H, DISPLAY_W_MM, DISPLAY_H_MM
        try:
            px_w = int(self.sb_disp_px_w.value())
            px_h = int(self.sb_disp_px_h.value())
            d_w = float(self.sb_disp_w_mm.value())
            d_h = float(self.sb_disp_h_mm.value())
            if px_w <= 0 or px_h <= 0 or d_w <= 0 or d_h <= 0:
                raise ValueError("All values must be > 0.")
        except Exception as e:
            if not silent:
                self._set_status(f"Invalid values: {e}", "error")
            return False

        DISPLAY_PIX_W = px_w
        DISPLAY_PIX_H = px_h
        DISPLAY_W_MM = d_w
        DISPLAY_H_MM = d_h
        recompute_scalars()

        if not silent:
            self._set_status(
                f"Settings applied: {DISPLAY_PIX_W}×{DISPLAY_PIX_H}px; "
                f"{DISPLAY_W_MM:.3f}×{DISPLAY_H_MM:.3f}mm.",
                "ok",
            )
        return True

    # ------------------------------------------------------------------
    # Prepare Output (worker thread)
    # ------------------------------------------------------------------
    def prepare_output(self):
        if self._prepare_thread and self._prepare_thread.isRunning():
            self._set_status("Already preparing…", "busy")
            return

        gds_path = self.gds_edit.text().strip()
        if not gds_path:
            self._set_status("No GDS file selected.", "error")
            return
        if not os.path.isfile(gds_path):
            self._set_status("GDS file not found.", "error")
            return

        if not self.apply_user_values():
            return

        # ensure cell/layer selected
        if self.combo_cell.count() == 0 or self.combo_layer.count() == 0:
            self._set_status("No cell/layer available.", "error")
            return

        cell_name = self.combo_cell.currentText()
        try:
            layer = int(self.combo_layer.currentText())
        except ValueError:
            self._set_status("Invalid layer selection.", "error")
            return

        # busy UI state
        self._set_status("Preparing…", "busy")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._set_controls_enabled(False)
        self.save_btn.setEnabled(False)

        # worker + thread
        self._prepare_worker = PrepareWorker(
            gds_path=gds_path,
            cell_name=cell_name,
            layer=layer,
            W_px=DISPLAY_PIX_W,
            H_px=DISPLAY_PIX_H,
            screen_w_mm=DISPLAY_W_MM,
            screen_h_mm=DISPLAY_H_MM,
            circle_diam_mm=CIRCLE_DIAM_MM,
            circle_offset_mm=CIRCLE_OFFSET_MM,
        )
        self._prepare_thread = QThread(self)
        self._prepare_worker.moveToThread(self._prepare_thread)

        self._prepare_thread.started.connect(self._prepare_worker.run)
        self._prepare_worker.finished.connect(self._prepare_finished)
        self._prepare_worker.error.connect(self._prepare_error)
        self._prepare_worker.finished.connect(self._prepare_thread.quit)
        self._prepare_worker.error.connect(self._prepare_thread.quit)
        self._prepare_thread.finished.connect(self._prepare_worker.deleteLater)
        self._prepare_thread.finished.connect(self._thread_cleanup)

        self._prepare_thread.start()

    def _thread_cleanup(self):
        QApplication.restoreOverrideCursor()
        self._set_controls_enabled(True)
        self._prepare_thread = None
        self._prepare_worker = None

    def _prepare_error(self, msg: str):
        print("Render error:", msg)
        self._set_status("Render error (see console).", "error")
        self.save_btn.setEnabled(False)
        self.preview_view.reset_placeholder()

    def _prepare_finished(self, pil_img: Image.Image):
        # store state
        self.image = pil_img

        # suggest output name if none given
        if not self.png_edit.text().strip() and self.gds_edit.text().strip():
            base = os.path.splitext(os.path.basename(self.gds_edit.text().strip()))[0] + ".png"
            out_path = os.path.join(os.path.dirname(self.gds_edit.text().strip()), base)
            self.png_edit.setText(out_path)

        # update preview
        pix = self._pil_to_qpixmap(self.image)
        self.preview_view.set_image_pixmap(pix)

        self._set_status("Output prepared. Click 'Save PNG' to write file.", "ok")
        self.save_btn.setEnabled(True)

        self.save_settings()

    # ------------------------------------------------------------------
    # Save PNG
    # ------------------------------------------------------------------
    def save_png(self):
        if self.image is None:
            self._set_status("Nothing to save – click 'Prepare Output' first.", "error")
            return

        fn = self.png_edit.text().strip()
        if not fn:
            fn, _ = QFileDialog.getSaveFileName(
                self, "Save PNG", "out.png", "PNG (*.png)"
            )
            if not fn:
                return
            if not fn.lower().endswith(".png"):
                fn += ".png"
        else:
            if not fn.lower().endswith(".png"):
                fn += ".png"

        try:
            self.image.save(fn, format="PNG")
            self._set_status(f"Saved: {os.path.basename(fn)}", "ok")
            self.save_settings()
        except Exception as e:
            print("Save error:", e)
            self._set_status("Save error (see console).", "error")

    # ------------------------------------------------------------------
    # PIL → QPixmap helper
    # ------------------------------------------------------------------
    def _pil_to_qpixmap(self, pil_image: Image.Image) -> QPixmap:
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        data = buf.getvalue()
        pix = QPixmap()
        pix.loadFromData(data, "PNG")
        return pix


# ------------------------------------------------------------------
def main():
    QCoreApplication.setOrganizationName(COMPANY_NAME)
    QCoreApplication.setApplicationName(APP_NAME_SETTINGS)

    app = QApplication(sys.argv)
    gui = MainGUI()
    gui.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()