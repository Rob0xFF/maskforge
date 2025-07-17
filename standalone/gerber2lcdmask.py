import sys, os, io, math
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QFileDialog,
    QVBoxLayout, QWidget, QCheckBox, QHBoxLayout, QSpinBox, QDoubleSpinBox,
    QLineEdit, QGroupBox, QGridLayout, QSizePolicy, QFrame, QGraphicsView,
    QGraphicsScene
)
from PyQt5.QtCore import (
    Qt, QCoreApplication, QSettings, QObject, QThread, pyqtSignal
)
from PyQt5.QtGui import QPixmap, QPainter, QColor
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None

from pygerber.gerberx3.api.v2 import GerberFile, ColorScheme, PixelFormatEnum, ImageFormatEnum
from pygerber.common.rgba import RGBA


# ------------------------------------------------------------------
# App identity / settings
# ------------------------------------------------------------------
COMPANY_NAME = "Rob0xFF"
APP_NAME_SETTINGS = "Gerber LCD Photomask"   # ASCII-safe name for QSettings
WINDOW_TITLE_DISPLAY = "Gerber \u2192 LCD Photomask"  # display title


# ------------------------------------------------------------------
# Default values (overridden via UI or QSettings load)
# ------------------------------------------------------------------
DISPLAY_PIX_W = 13312
DISPLAY_PIX_H = 5120
DISPLAY_W_MM   = 223.642
DISPLAY_H_MM   = 126.48
PCB_W_MM       = 160.0
PCB_H_MM       = 100.0

# Derived (updated in recompute_scalars)
PX_PER_MM_X = None
PX_PER_MM_Y = None
DRAW_W = None
DRAW_H = None


def recompute_scalars():
    """Compute derived scale factors and drawing dimensions."""
    global PX_PER_MM_X, PX_PER_MM_Y, DRAW_W, DRAW_H
    PX_PER_MM_X = DISPLAY_PIX_W / DISPLAY_W_MM
    PX_PER_MM_Y = DISPLAY_PIX_H / DISPLAY_H_MM
    DRAW_W = math.ceil(PCB_W_MM * PX_PER_MM_X)
    DRAW_H = math.ceil(PCB_H_MM * PX_PER_MM_Y)


# initial compute
recompute_scalars()


# ------------------------------------------------------------------
# Binary color scheme
# ------------------------------------------------------------------
binary_scheme = ColorScheme(
    background_color=RGBA.from_rgba(255, 255, 255, 255),
    clear_color=RGBA.from_rgba(255, 255, 255, 255),
    solid_color=RGBA.from_rgba(0, 0, 0, 255),
    clear_region_color=RGBA.from_rgba(255, 255, 255, 255),
    solid_region_color=RGBA.from_rgba(0, 0, 0, 255),
)


# ------------------------------------------------------------------
# Rendering helpers (PNG-Erzeugung unverändert: LANCZOS, Invert-Bugfix)
# ------------------------------------------------------------------
def render_bw_with_origin(path: str):
    parsed = GerberFile.from_file(path).parse()
    info = parsed.get_info()

    buf = io.BytesIO()
    parsed.render_raster(
        destination=buf,
        color_scheme=binary_scheme,
        image_format=ImageFormatEnum.PNG,
        dpmm=int(PX_PER_MM_X * 2),
        pixel_format=PixelFormatEnum.RGBA,
    )
    buf.seek(0)
    bw = Image.open(buf).convert("L")

    return bw, info.min_x_mm, info.max_y_mm, info.width_mm, info.height_mm


def build_canvas(img: Image.Image, invert: bool, mirror: bool, min_x_mm, max_y_mm, w, h) -> Image.Image:
    # ORIGINAL behavior: LANCZOS
    img = img.resize(
        (int(PX_PER_MM_X * float(w)), int(PX_PER_MM_Y * float(h))),
        resample=Image.LANCZOS,
    )

    canvas_pcb = Image.new("L", (DRAW_W, DRAW_H), 255)

    offset_x = round(float(min_x_mm) * PX_PER_MM_X)
    offset_y = round(-(float(max_y_mm)) * PX_PER_MM_Y)
    canvas_pcb.paste(img, (offset_x, offset_y))

    if mirror:
        canvas_pcb = ImageOps.mirror(canvas_pcb)
    if invert:
        canvas_pcb = ImageOps.invert(canvas_pcb)  # bugfix retained

    canvas = Image.new("L", (DISPLAY_PIX_W, DISPLAY_PIX_H), 0)

    x = (DISPLAY_PIX_W - DRAW_W) // 2
    y = (DISPLAY_PIX_H - DRAW_H) // 2

    canvas.paste(canvas_pcb, (x, y))
    return canvas


# ------------------------------------------------------------------
# Worker thread to offload heavy Prepare work
# ------------------------------------------------------------------
class PrepareWorker(QObject):
    finished = pyqtSignal(Image.Image, float, float, float, float)  # pil_img, min_x, max_y, w, h
    error = pyqtSignal(str)

    def __init__(self, gerber_path: str, invert: bool, mirror: bool):
        super().__init__()
        self.gerber_path = gerber_path
        self.invert = invert
        self.mirror = mirror

    def run(self):
        try:
            img0, min_x, max_y, w, h = render_bw_with_origin(self.gerber_path)
            canvas_img = build_canvas(
                img0, self.invert, self.mirror, min_x, max_y, w, h
            )
        except Exception as e:
            self.error.emit(str(e))
            return

        self.finished.emit(canvas_img, min_x, max_y, w, h)


# ------------------------------------------------------------------
# Interactive Preview View (hard-pixel display; no smoothing)
# ------------------------------------------------------------------
class PreviewView(QGraphicsView):
    """
    Mouse-wheel zoomable preview.
    - Wheel: zoom in/out around cursor
    - Drag: pan
    - Double-click: fit
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

        # ---- Updated zoom limits ----
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
        self.min_x = self.max_y = self.width = self.height = None
        self.gerber_path = None

        # thread refs
        self._prepare_thread = None
        self._prepare_worker = None

        # ------------------------------------------------------------------
        # Files group
        # ------------------------------------------------------------------
        self.gerber_edit = QLineEdit()
        self.gerber_edit.setPlaceholderText("Select Gerber file…")
        self.gerber_browse_btn = QPushButton("Browse...")
        self.gerber_browse_btn.clicked.connect(self.browse_gerber)

        self.png_edit = QLineEdit()
        self.png_edit.setPlaceholderText("Select output PNG…")
        self.png_browse_btn = QPushButton("Browse...")
        self.png_browse_btn.clicked.connect(self.browse_png)

        files_grid = QGridLayout()
        lbl_gerber = QLabel("Gerber:")
        lbl_gerber.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_png = QLabel("Output PNG:")
        lbl_png.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        files_grid.addWidget(lbl_gerber, 0, 0)
        files_grid.addWidget(self.gerber_edit, 0, 1)
        files_grid.addWidget(self.gerber_browse_btn, 0, 2)

        files_grid.addWidget(lbl_png, 1, 0)
        files_grid.addWidget(self.png_edit, 1, 1)
        files_grid.addWidget(self.png_browse_btn, 1, 2)

        files_grid.setColumnStretch(0, 0)
        files_grid.setColumnStretch(1, 1)
        files_grid.setColumnStretch(2, 0)

        files_group = QGroupBox("Files")
        files_group.setLayout(files_grid)

        # ------------------------------------------------------------------
        # Settings group
        # ------------------------------------------------------------------
        self.sb_disp_px_w = QSpinBox(); self.sb_disp_px_w.setRange(1, 200000); self.sb_disp_px_w.setAlignment(Qt.AlignRight); self.sb_disp_px_w.setFixedWidth(self.FIELD_WIDTH)
        self.sb_disp_px_h = QSpinBox(); self.sb_disp_px_h.setRange(1, 200000); self.sb_disp_px_h.setAlignment(Qt.AlignRight); self.sb_disp_px_h.setFixedWidth(self.FIELD_WIDTH)
        self.sb_disp_w_mm = QDoubleSpinBox(); self.sb_disp_w_mm.setDecimals(3); self.sb_disp_w_mm.setRange(0.001, 10000); self.sb_disp_w_mm.setAlignment(Qt.AlignRight); self.sb_disp_w_mm.setFixedWidth(self.FIELD_WIDTH)
        self.sb_disp_h_mm = QDoubleSpinBox(); self.sb_disp_h_mm.setDecimals(3); self.sb_disp_h_mm.setRange(0.001, 10000); self.sb_disp_h_mm.setAlignment(Qt.AlignRight); self.sb_disp_h_mm.setFixedWidth(self.FIELD_WIDTH)
        self.sb_pcb_w_mm  = QDoubleSpinBox(); self.sb_pcb_w_mm.setDecimals(3);  self.sb_pcb_w_mm.setRange(0.001, 10000);  self.sb_pcb_w_mm.setAlignment(Qt.AlignRight); self.sb_pcb_w_mm.setFixedWidth(self.FIELD_WIDTH)
        self.sb_pcb_h_mm  = QDoubleSpinBox(); self.sb_pcb_h_mm.setDecimals(3);  self.sb_pcb_h_mm.setRange(0.001, 10000);  self.sb_pcb_h_mm.setAlignment(Qt.AlignRight); self.sb_pcb_h_mm.setFixedWidth(self.FIELD_WIDTH)

        self.chk_inv = QCheckBox("Invert")
        self.chk_mir = QCheckBox("Mirror (top layer)")
        self.chk_mir.setChecked(True)

        settings_grid = QGridLayout()
        row = 0
        settings_grid.addWidget(self._mk_label("Display Width (px)"),  row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_px_w,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("Display Height (px)"), row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_px_h,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("Display Width (mm)"),  row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_w_mm,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("Display Height (mm)"), row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_disp_h_mm,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("PCB Width (mm)"),      row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_pcb_w_mm,  row, 2, Qt.AlignRight); row += 1
        settings_grid.addWidget(self._mk_label("PCB Height (mm)"),     row, 0, Qt.AlignLeft); settings_grid.addWidget(self.sb_pcb_h_mm,  row, 2, Qt.AlignRight); row += 1

        settings_grid.setColumnStretch(0, 0)
        settings_grid.setColumnStretch(1, 1)
        settings_grid.setColumnStretch(2, 0)

        chk_row = QHBoxLayout()
        chk_row.addWidget(self.chk_inv)
        chk_row.addWidget(self.chk_mir)
        chk_row.addStretch(1)

        settings_vlayout = QVBoxLayout()
        settings_vlayout.addLayout(settings_grid)
        settings_vlayout.addLayout(chk_row)

        settings_group = QGroupBox("Settings")
        settings_group.setLayout(settings_vlayout)

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
        # Status row with colored indicator (spacing reduced to 4px)
        # ------------------------------------------------------------------
        self.ind = QLabel()
        self.ind.setFixedSize(10, 10)
        self.ind.setStyleSheet("border-radius:5px;background:#4caf50;")  # init green

        self.status_label = QLabel("")

        status_row = QHBoxLayout()
        status_row.addWidget(self.ind)
        status_row.addSpacing(4)  # was 6
        status_row.addWidget(self.status_label, 1)
        status_row.addStretch(0)

        # ------------------------------------------------------------------
        # Left control panel
        # ------------------------------------------------------------------
        controls_layout = QVBoxLayout()
        controls_layout.addWidget(files_group)
        controls_layout.addWidget(settings_group)
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
        self.gerber_edit.setEnabled(enabled)
        self.gerber_browse_btn.setEnabled(enabled)
        self.png_edit.setEnabled(enabled)
        self.png_browse_btn.setEnabled(enabled)
        self.sb_disp_px_w.setEnabled(enabled)
        self.sb_disp_px_h.setEnabled(enabled)
        self.sb_disp_w_mm.setEnabled(enabled)
        self.sb_disp_h_mm.setEnabled(enabled)
        self.sb_pcb_w_mm.setEnabled(enabled)
        self.sb_pcb_h_mm.setEnabled(enabled)
        self.chk_inv.setEnabled(enabled)
        self.chk_mir.setEnabled(enabled)
        self.prepare_btn.setEnabled(enabled)
        # save_btn separat

    # ------------------------------------------------------------------
    # QSettings load/save
    # ------------------------------------------------------------------
    def load_settings(self):
        gerber_path = self.settings.value("paths/gerber", "", type=str)
        png_path    = self.settings.value("paths/output_png", "", type=str)
        self.gerber_edit.setText(gerber_path)
        self.png_edit.setText(png_path)

        px_w = self.settings.value("display/pix_w", DISPLAY_PIX_W, type=int)
        px_h = self.settings.value("display/pix_h", DISPLAY_PIX_H, type=int)
        d_w  = self.settings.value("display/mm_w",  DISPLAY_W_MM, type=float)
        d_h  = self.settings.value("display/mm_h",  DISPLAY_H_MM, type=float)
        p_w  = self.settings.value("pcb/mm_w",      PCB_W_MM, type=float)
        p_h  = self.settings.value("pcb/mm_h",      PCB_H_MM, type=float)

        self.sb_disp_px_w.setValue(px_w)
        self.sb_disp_px_h.setValue(px_h)
        self.sb_disp_w_mm.setValue(d_w)
        self.sb_disp_h_mm.setValue(d_h)
        self.sb_pcb_w_mm.setValue(p_w)
        self.sb_pcb_h_mm.setValue(p_h)

        inv = self.settings.value("options/invert", False, type=bool)
        mir = self.settings.value("options/mirror", True,  type=bool)
        self.chk_inv.setChecked(inv)
        self.chk_mir.setChecked(mir)

        # apply to globals (no rendering)
        self.apply_user_values(silent=True)

    def save_settings(self):
        self.settings.setValue("paths/gerber",     self.gerber_edit.text().strip())
        self.settings.setValue("paths/output_png", self.png_edit.text().strip())
        self.settings.setValue("display/pix_w", self.sb_disp_px_w.value())
        self.settings.setValue("display/pix_h", self.sb_disp_px_h.value())
        self.settings.setValue("display/mm_w",  self.sb_disp_w_mm.value())
        self.settings.setValue("display/mm_h",  self.sb_disp_h_mm.value())
        self.settings.setValue("pcb/mm_w", self.sb_pcb_w_mm.value())
        self.settings.setValue("pcb/mm_h", self.sb_pcb_h_mm.value())
        self.settings.setValue("options/invert", self.chk_inv.isChecked())
        self.settings.setValue("options/mirror", self.chk_mir.isChecked())
        self.settings.sync()

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # File handlers
    # ------------------------------------------------------------------
    def browse_gerber(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Select Gerber file", "", "Gerber (*.gbr *.gtl *.gbl)"
        )
        if fn:
            self.gerber_edit.setText(fn)
            self.gerber_path = fn
            self._set_status("Gerber file selected (not processed yet).", "ok")
            self.save_settings()

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
        global DISPLAY_PIX_W, DISPLAY_PIX_H, DISPLAY_W_MM, DISPLAY_H_MM, PCB_W_MM, PCB_H_MM
        try:
            px_w = int(self.sb_disp_px_w.value())
            px_h = int(self.sb_disp_px_h.value())
            d_w = float(self.sb_disp_w_mm.value())
            d_h = float(self.sb_disp_h_mm.value())
            p_w = float(self.sb_pcb_w_mm.value())
            p_h = float(self.sb_pcb_h_mm.value())
            if px_w <= 0 or px_h <= 0 or d_w <= 0 or d_h <= 0 or p_w <= 0 or p_h <= 0:
                raise ValueError("All values must be > 0.")
        except Exception as e:
            if not silent:
                self._set_status(f"Invalid values: {e}", "error")
            return False

        DISPLAY_PIX_W = px_w
        DISPLAY_PIX_H = px_h
        DISPLAY_W_MM = d_w
        DISPLAY_H_MM = d_h
        PCB_W_MM = p_w
        PCB_H_MM = p_h
        recompute_scalars()

        if not silent:
            self._set_status(
                f"Settings applied: {DISPLAY_PIX_W}×{DISPLAY_PIX_H}px display; "
                f"{DISPLAY_W_MM:.3f}×{DISPLAY_H_MM:.3f}mm; "
                f"PCB {PCB_W_MM:.3f}×{PCB_H_MM:.3f}mm.",
                "ok",
            )
        return True

    # ------------------------------------------------------------------
    # Prepare Output (non-blocking via worker thread; no overlay)
    # ------------------------------------------------------------------
    def prepare_output(self):
        if self._prepare_thread and self._prepare_thread.isRunning():
            self._set_status("Already preparing…", "busy")
            return

        gerber_path = self.gerber_edit.text().strip()
        if not gerber_path:
            self._set_status("No Gerber file selected.", "error")
            return
        if not os.path.isfile(gerber_path):
            self._set_status("Gerber file not found.", "error")
            return

        if not self.apply_user_values():
            return

        # busy UI state
        self._set_status("Preparing…", "busy")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._set_controls_enabled(False)
        self.save_btn.setEnabled(False)

        # worker + thread
        self._prepare_worker = PrepareWorker(
            gerber_path=gerber_path,
            invert=self.chk_inv.isChecked(),
            mirror=self.chk_mir.isChecked(),
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

    def _prepare_finished(self, pil_img: Image.Image, min_x, max_y, w, h):
        # store state
        self.image = pil_img
        self.min_x, self.max_y, self.width, self.height = min_x, max_y, w, h

        # suggest output name if none given
        if not self.png_edit.text().strip() and self.gerber_edit.text().strip():
            base = os.path.splitext(os.path.basename(self.gerber_edit.text().strip()))[0] + ".png"
            out_path = os.path.join(os.path.dirname(self.gerber_edit.text().strip()), base)
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