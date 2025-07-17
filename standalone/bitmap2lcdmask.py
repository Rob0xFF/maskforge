import sys
import os
import io
import math

from PIL import Image, ImageDraw, ImageOps
# Pillow API compat: LANCZOS location changed in newer versions
try:
    RES_LANCZOS = Image.Resampling.LANCZOS  # Pillow >= 10
except AttributeError:
    RES_LANCZOS = Image.LANCZOS             # older Pillow

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QFileDialog,
    QVBoxLayout, QWidget, QHBoxLayout, QSpinBox, QDoubleSpinBox,
    QLineEdit, QGroupBox, QGridLayout, QSizePolicy, QFrame, QGraphicsView,
    QGraphicsScene
)
from PyQt5.QtCore import (
    Qt, QCoreApplication, QSettings, QObject, QThread, pyqtSignal
)
from PyQt5.QtGui import QPixmap, QPainter, QColor


# ------------------------------------------------------------------
# App identity / settings
# ------------------------------------------------------------------
COMPANY_NAME = "Rob0xFF"
APP_NAME_SETTINGS = "Bitmap LCD Photomask"   # ASCII-safe for QSettings
WINDOW_TITLE_DISPLAY = "Bitmap \u2192 LCD Photomask"


# ------------------------------------------------------------------
# Default values (overridden by QSettings)
# ------------------------------------------------------------------
DISPLAY_PIX_W = 13312
DISPLAY_PIX_H = 5120
DISPLAY_W_MM   = 223.642
DISPLAY_H_MM   = 126.48

# Kreis- & Platzierungs-Parameter wie in deiner Vorlage
CIRCLE_DIAM_MM   = 100.0   # -> Radius 50 mm
CIRCLE_OFFSET_MM = 60.0    # Abstand Zentrum vom linken/rechten Rand (mm)

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
# Core rendering — funktional identisch zu deinem Originalcode
# (Bild links normal, rechts invertiert, globales Mirror).
# ------------------------------------------------------------------
def render_bitmap_to_photomask(
    input_path: str,
    threshold: int,
    W_px: int,
    H_px: int,
    screen_width_mm: float,
    screen_height_mm: float,
    circle_diam_mm: float = CIRCLE_DIAM_MM,
    circle_offset_mm: float = CIRCLE_OFFSET_MM,
):
    """
    Render ein Eingangsbitmap in das LCD-Format mit zwei Kreisprojektionen
    (links normal, rechts invertiert, dann Gesamtbild spiegeln).
    Gibt ein PIL Image (L) zurück.
    """
    # Display-Skalierung
    px_per_mm_x = W_px / screen_width_mm
    px_per_mm_y = H_px / screen_height_mm

    # Kreis-Radien
    radius_mm = circle_diam_mm / 2.0
    radius_px_x = int(radius_mm * px_per_mm_x)
    radius_px_y = int(radius_mm * px_per_mm_y)

    center_y = H_px // 2
    left_center_x = int(circle_offset_mm * px_per_mm_x)
    right_center_x = int((screen_width_mm - circle_offset_mm) * px_per_mm_x)

    # Basis-LCD-Bild
    base = Image.new("L", (W_px, H_px), 0)

    # Eingangsladen
    img = Image.open(input_path)

    # Links (nicht invertiert)
    place_in_circle(
        base_img=base,
        content_img=img,
        center=(left_center_x, center_y),
        radius_px_x=radius_px_x,
        radius_px_y=radius_px_y,
        threshold=threshold,
        invert=False,
    )

    # Rechts (invertiert)
    place_in_circle(
        base_img=base,
        content_img=img,
        center=(right_center_x, center_y),
        radius_px_x=radius_px_x,
        radius_px_y=radius_px_y,
        threshold=threshold,
        invert=True,
    )

    # Gesamtspiegelung (wie in deiner Vorlage)
    base = ImageOps.mirror(base)

    return base


def place_in_circle(base_img, content_img, center, radius_px_x, radius_px_y, threshold=None, invert=False):
    """
    Unveränderte Projektion aus deinem Original:
    - Graustufe
    - Threshold binär
    - ggf. invertieren
    - zu quadratischem Ausschnitt croppen
    - LANCZOS auf Ellipsegröße
    - elliptische Maskierung & Einfügen
    """
    content_img = content_img.convert("L")

    if threshold is not None:
        thr = int(threshold)
        content_img = content_img.point(lambda x: 255 if x >= thr else 0, mode='L')

    if invert:
        content_img = ImageOps.invert(content_img)

    # Quadratischen Ausschnitt (zentriert) wählen
    min_side = min(content_img.size)
    left = (content_img.width  - min_side) // 2
    top  = (content_img.height - min_side) // 2
    content_cropped = content_img.crop((left, top, left + min_side, top + min_side))

    # Skalieren auf Ellipsengröße
    scaled = content_cropped.resize(
        (2 * radius_px_x, 2 * radius_px_y),
        resample=RES_LANCZOS,
    )

    # Kreisförmige (elliptische) Maske
    mask = Image.new("L", (2 * radius_px_x, 2 * radius_px_y), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse([0, 0, 2 * radius_px_x, 2 * radius_px_y], fill=255)

    paste_position = (center[0] - radius_px_x, center[1] - radius_px_y)
    base_img.paste(scaled, paste_position, mask)


# ------------------------------------------------------------------
# Worker thread: schwere Renderarbeit offloaden
# ------------------------------------------------------------------
class PrepareWorker(QObject):
    finished = pyqtSignal(Image.Image)  # final photomask image
    error = pyqtSignal(str)

    def __init__(self, input_path: str, threshold: int,
                 W_px: int, H_px: int,
                 screen_w_mm: float, screen_h_mm: float,
                 circle_diam_mm: float, circle_offset_mm: float):
        super().__init__()
        self.input_path = input_path
        self.threshold = threshold
        self.W_px = W_px
        self.H_px = H_px
        self.screen_w_mm = screen_w_mm
        self.screen_h_mm = screen_h_mm
        self.circle_diam_mm = circle_diam_mm
        self.circle_offset_mm = circle_offset_mm

    def run(self):
        try:
            img = render_bitmap_to_photomask(
                input_path=self.input_path,
                threshold=self.threshold,
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

        # Zoom limits (wie angefordert)
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
        self.image_path = None

        # thread refs
        self._prepare_thread = None
        self._prepare_worker = None

        # ------------------------------------------------------------------
        # Files group
        # ------------------------------------------------------------------
        self.bmp_edit = QLineEdit()
        self.bmp_edit.setPlaceholderText("Select bitmap…")
        self.bmp_browse_btn = QPushButton("Browse...")
        self.bmp_browse_btn.clicked.connect(self.browse_bitmap)

        self.png_edit = QLineEdit()
        self.png_edit.setPlaceholderText("Select output PNG…")
        self.png_browse_btn = QPushButton("Browse...")
        self.png_browse_btn.clicked.connect(self.browse_png)

        files_grid = QGridLayout()
        lbl_bmp = QLabel("Bitmap:")
        lbl_bmp.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_png = QLabel("Output PNG:")
        lbl_png.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        files_grid.addWidget(lbl_bmp, 0, 0)
        files_grid.addWidget(self.bmp_edit, 0, 1)
        files_grid.addWidget(self.bmp_browse_btn, 0, 2)

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
        # Threshold group
        # ------------------------------------------------------------------
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(0, 254)
        self.spin_threshold.setValue(128)
        self.spin_threshold.setAlignment(Qt.AlignRight)
        self.spin_threshold.setFixedWidth(self.FIELD_WIDTH)

        threshold_grid = QGridLayout()
        threshold_grid.addWidget(self._mk_label("Threshold (0–254)"), 0, 0, Qt.AlignLeft)
        threshold_grid.addWidget(self.spin_threshold,                0, 2, Qt.AlignRight)

        threshold_grid.setColumnStretch(0, 0)
        threshold_grid.setColumnStretch(1, 1)
        threshold_grid.setColumnStretch(2, 0)

        threshold_group = QGroupBox("Threshold")
        threshold_group.setLayout(threshold_grid)

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
        controls_layout.addWidget(threshold_group)
        controls_layout.addLayout(buttons_row)
        controls_layout.addLayout(status_row)
        controls_layout.addStretch(1)
        controls_widget = QWidget(); controls_widget.setLayout(controls_layout)

        # ------------------------------------------------------------------
        # Preview panel (right column)
        # ------------------------------------------------------------------
        self.preview_view = PreviewView(self.PREVIEW_W, self.PREVIEW_H)
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
        self.bmp_edit.setEnabled(enabled)
        self.bmp_browse_btn.setEnabled(enabled)
        self.png_edit.setEnabled(enabled)
        self.png_browse_btn.setEnabled(enabled)
        self.sb_disp_px_w.setEnabled(enabled)
        self.sb_disp_px_h.setEnabled(enabled)
        self.sb_disp_w_mm.setEnabled(enabled)
        self.sb_disp_h_mm.setEnabled(enabled)
        self.spin_threshold.setEnabled(enabled)
        self.prepare_btn.setEnabled(enabled)
        # save_btn separat

    # ------------------------------------------------------------------
    # QSettings load/save
    # ------------------------------------------------------------------
    def load_settings(self):
        bmp_path = self.settings.value("paths/bitmap", "", type=str)
        png_path = self.settings.value("paths/output_png", "", type=str)
        self.bmp_edit.setText(bmp_path)
        self.png_edit.setText(png_path)

        px_w = self.settings.value("display/pix_w", DISPLAY_PIX_W, type=int)
        px_h = self.settings.value("display/pix_h", DISPLAY_PIX_H, type=int)
        d_w  = self.settings.value("display/mm_w",  DISPLAY_W_MM, type=float)
        d_h  = self.settings.value("display/mm_h",  DISPLAY_H_MM, type=float)
        thr  = self.settings.value("threshold/value", 128, type=int)

        self.sb_disp_px_w.setValue(px_w)
        self.sb_disp_px_h.setValue(px_h)
        self.sb_disp_w_mm.setValue(d_w)
        self.sb_disp_h_mm.setValue(d_h)
        self.spin_threshold.setValue(thr)

        if bmp_path and os.path.isfile(bmp_path):
            self.image_path = bmp_path  # remember loaded

        # apply to globals (no rendering)
        self.apply_user_values(silent=True)

    def save_settings(self):
        self.settings.setValue("paths/bitmap",     self.bmp_edit.text().strip())
        self.settings.setValue("paths/output_png", self.png_edit.text().strip())
        self.settings.setValue("display/pix_w", self.sb_disp_px_w.value())
        self.settings.setValue("display/pix_h", self.sb_disp_px_h.value())
        self.settings.setValue("display/mm_w",  self.sb_disp_w_mm.value())
        self.settings.setValue("display/mm_h",  self.sb_disp_h_mm.value())
        self.settings.setValue("threshold/value", self.spin_threshold.value())
        self.settings.sync()

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Bitmap load (UI action)
    # ------------------------------------------------------------------
    def browse_bitmap(self):
        fn, _ = QFileDialog.getOpenFileName(
            self,
            "Select bitmap",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff)"
        )
        if fn:
            self.bmp_edit.setText(fn)
            self.image_path = fn
            self._set_status("Bitmap selected (not processed yet).", "ok")
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

        img_path = self.bmp_edit.text().strip()
        if not img_path:
            self._set_status("No bitmap selected.", "error")
            return
        if not os.path.isfile(img_path):
            self._set_status("Bitmap file not found.", "error")
            return

        if not self.apply_user_values():
            return

        threshold = self.spin_threshold.value()

        # busy UI state
        self._set_status("Preparing…", "busy")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._set_controls_enabled(False)
        self.save_btn.setEnabled(False)

        # worker + thread
        self._prepare_worker = PrepareWorker(
            input_path=img_path,
            threshold=threshold,
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
        if not self.png_edit.text().strip() and self.bmp_edit.text().strip():
            base = os.path.splitext(os.path.basename(self.bmp_edit.text().strip()))[0] + ".png"
            out_path = os.path.join(os.path.dirname(self.bmp_edit.text().strip()), base)
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