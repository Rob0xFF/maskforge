#!/usr/bin/env python3
"""
Photomask Toolkit
=================
Multi-tool GUI (PyQt5) for generating LCD photomask PNGs from:
  • Gerber files
  • GDSII layouts
  • Arbitrary bitmaps (with threshold + live preview)

Shared display geometry settings (px + mm) propagate across all tools.
Each tool/tab renders off the UI thread via QThread/Worker.
Preview pane (right) shows most recent render; zoom/pan; hard pixels.

Company: Rob0xFF
"""

import sys
import os
import io
import math
import uuid
from typing import Optional

import gdspy  # required for GDS tab
from PIL import Image, ImageDraw, ImageOps
try:
    RES_LANCZOS = Image.Resampling.LANCZOS  # Pillow >=10
except AttributeError:  # Pillow <10 fallback
    RES_LANCZOS = Image.LANCZOS

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QLabel, QPushButton, QFileDialog,
    QVBoxLayout, QHBoxLayout, QSpinBox, QDoubleSpinBox,
    QLineEdit, QGroupBox, QGridLayout, QSizePolicy,
    QFrame, QGraphicsView, QGraphicsScene, QComboBox, QCheckBox
)
from PyQt5.QtCore import (
    Qt, QCoreApplication, QSettings, QTimer,
    QObject, QThread, pyqtSignal
)
from PyQt5.QtGui import QPixmap, QPainter, QColor

# ---------------- pygerber (optional) ----------------
try:
    from pygerber.gerberx3.api.v2 import GerberFile, ColorScheme, PixelFormatEnum, ImageFormatEnum
    from pygerber.common.rgba import RGBA
    HAVE_PYGERBER = True
except Exception:
    HAVE_PYGERBER = False

Image.MAX_IMAGE_PIXELS = None  # allow very large images


# ==================================================================
# Global App Identity / Settings
# ==================================================================
COMPANY_NAME = "Rob0xFF"
APP_NAME_SETTINGS = "Maskforge Toolkit"
WINDOW_TITLE_DISPLAY = "Maskforge Toolkit (Gerber / GDS / Bitmap)"


# ==================================================================
# Global Display Model (shared px/mm across tabs)
# ==================================================================
class DisplayModel(QObject):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._pix_w = 13312
        self._pix_h = 5120
        self._mm_w  = 223.642
        self._mm_h  = 126.48

    @property
    def pix_w(self): return self._pix_w
    @property
    def pix_h(self): return self._pix_h
    @property
    def mm_w(self):  return self._mm_w
    @property
    def mm_h(self):  return self._mm_h

    def set_values(self, pix_w: int, pix_h: int, mm_w: float, mm_h: float):
        changed = (
            pix_w != self._pix_w or pix_h != self._pix_h or
            mm_w  != self._mm_w  or mm_h  != self._mm_h
        )
        self._pix_w = pix_w
        self._pix_h = pix_h
        self._mm_w  = mm_w
        self._mm_h  = mm_h
        if changed:
            self.changed.emit()

    def px_per_mm_x(self) -> float:
        return self._pix_w / self._mm_w

    def px_per_mm_y(self) -> float:
        return self._pix_h / self._mm_h


DISPLAY_MODEL = DisplayModel()  # singleton-ish


# ==================================================================
# Shared Status Widget (indicator + label)
# ==================================================================
class StatusRow(QWidget):
    COLOR_OK   = "#4caf50"
    COLOR_ERR  = "#f44336"
    COLOR_BUSY = "#2196f3"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ind = QLabel()
        self.ind.setFixedSize(10, 10)
        self.ind.setStyleSheet(f"border-radius:5px;background:{self.COLOR_OK};")
        self.label = QLabel("")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ind)
        layout.addSpacing(4)
        layout.addWidget(self.label, 1)
        layout.addStretch(0)

    def set_status(self, text: str, state: str):
        color = self.COLOR_OK
        if state in ("busy", "preparing"):
            color = self.COLOR_BUSY
        elif state in ("error", "err", "fail"):
            color = self.COLOR_ERR
        self.ind.setStyleSheet(f"border-radius:5px;background:{color};")
        self.label.setText(text)


# ==================================================================
# Shared Preview View (zoom/pan; hard pixels)
# ==================================================================
class PreviewView(QGraphicsView):
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

        self._min_scale = 0.05   # per user
        self._max_scale = 10.0

    def _make_placeholder_pixmap(self) -> QPixmap:
        pix = QPixmap(self._w, self._h)
        pix.fill(QColor("#303030"))
        painter = QPainter(pix)
        painter.setPen(QColor("#b0b0b0"))
        font = painter.font(); font.setPointSize(24); painter.setFont(font)
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


# ==================================================================
# Utility: PIL → QPixmap
# ==================================================================

def pil_to_qpixmap(pil_image: Image.Image) -> QPixmap:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    pix = QPixmap()
    pix.loadFromData(buf.getvalue(), "PNG")
    return pix


# ==================================================================
# ----------------------- GERBER TAB --------------------------------
# ==================================================================
if HAVE_PYGERBER:
    _binary_scheme = ColorScheme(
        background_color=RGBA.from_rgba(255, 255, 255, 255),
        clear_color=RGBA.from_rgba(255, 255, 255, 255),
        solid_color=RGBA.from_rgba(0, 0, 0, 255),
        clear_region_color=RGBA.from_rgba(255, 255, 255, 255),
        solid_region_color=RGBA.from_rgba(0, 0, 0, 255),
    )
else:
    _binary_scheme = None


def _gerber_render_bw_with_origin(path: str, px_per_mm_x: float):
    if not HAVE_PYGERBER:
        raise RuntimeError("pygerber not available.")
    parsed = GerberFile.from_file(path).parse()
    info = parsed.get_info()
    buf = io.BytesIO()
    parsed.render_raster(
        destination=buf,
        color_scheme=_binary_scheme,
        image_format=ImageFormatEnum.PNG,
        dpmm=int(px_per_mm_x * 2),
        pixel_format=PixelFormatEnum.RGBA,
    )
    buf.seek(0)
    bw = Image.open(buf).convert("L")
    return bw, info.min_x_mm, info.max_y_mm, info.width_mm, info.height_mm


def _gerber_build_canvas(
    img: Image.Image,
    invert: bool,
    mirror: bool,
    min_x_mm: float,
    max_y_mm: float,
    w_mm: float,
    h_mm: float,
    disp_pix_w: int,
    disp_pix_h: int,
    px_per_mm_x: float,
    px_per_mm_y: float,
    pcb_w_mm: float,
    pcb_h_mm: float,
) -> Image.Image:
    img = img.resize(
        (int(px_per_mm_x * float(w_mm)), int(px_per_mm_y * float(h_mm))),
        resample=RES_LANCZOS,
    )
    draw_w = math.ceil(pcb_w_mm * px_per_mm_x)
    draw_h = math.ceil(pcb_h_mm * px_per_mm_y)
    canvas_pcb = Image.new("L", (draw_w, draw_h), 255)

    offset_x = round(float(min_x_mm) * px_per_mm_x)
    offset_y = round(-(float(max_y_mm)) * px_per_mm_y)
    canvas_pcb.paste(img, (offset_x, offset_y))

    if mirror:
        canvas_pcb = ImageOps.mirror(canvas_pcb)
    if invert:
        canvas_pcb = ImageOps.invert(canvas_pcb)

    canvas = Image.new("L", (disp_pix_w, disp_pix_h), 0)
    x = (disp_pix_w - draw_w) // 2
    y = (disp_pix_h - draw_h) // 2
    canvas.paste(canvas_pcb, (x, y))
    return canvas


class GerberWorker(QObject):
    finished = pyqtSignal(Image.Image)
    error = pyqtSignal(str)

    def __init__(self, path: str, invert: bool, mirror: bool,
                 disp_pix_w: int, disp_pix_h: int,
                 disp_mm_w: float, disp_mm_h: float,
                 pcb_mm_w: float, pcb_mm_h: float):
        super().__init__()
        self.path = path
        self.invert = invert
        self.mirror = mirror
        self.disp_pix_w = disp_pix_w
        self.disp_pix_h = disp_pix_h
        self.disp_mm_w = disp_mm_w
        self.disp_mm_h = disp_mm_h
        self.pcb_mm_w = pcb_mm_w
        self.pcb_mm_h = pcb_mm_h

    def run(self):
        try:
            px_per_mm_x = self.disp_pix_w / self.disp_mm_w
            px_per_mm_y = self.disp_pix_h / self.disp_mm_h
            img0, min_x, max_y, w_mm, h_mm = _gerber_render_bw_with_origin(self.path, px_per_mm_x)
            canvas_img = _gerber_build_canvas(
                img=img0,
                invert=self.invert,
                mirror=self.mirror,
                min_x_mm=min_x,
                max_y_mm=max_y,
                w_mm=w_mm,
                h_mm=h_mm,
                disp_pix_w=self.disp_pix_w,
                disp_pix_h=self.disp_pix_h,
                px_per_mm_x=px_per_mm_x,
                px_per_mm_y=px_per_mm_y,
                pcb_w_mm=self.pcb_mm_w,
                pcb_h_mm=self.pcb_mm_h,
            )
        except Exception as e:
            self.error.emit(str(e))
            return
        self.finished.emit(canvas_img)


class GerberTab(QWidget):
    def __init__(self, settings: QSettings, preview_callback, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.preview_callback = preview_callback

        self.image: Optional[Image.Image] = None
        self.gerber_path: Optional[str] = None

        self._prepare_thread = None
        self._prepare_worker = None

        self._pcb_w_mm_default = 160.0
        self._pcb_h_mm_default = 100.0

        # Files
        self.gerber_edit = QLineEdit(); self.gerber_edit.setPlaceholderText("Select Gerber file…")
        self.gerber_browse_btn = QPushButton("Browse..."); self.gerber_browse_btn.clicked.connect(self.browse_gerber)
        self.png_edit = QLineEdit(); self.png_edit.setPlaceholderText("Select output PNG…")
        self.png_browse_btn = QPushButton("Browse..."); self.png_browse_btn.clicked.connect(self.browse_png)

        files_grid = QGridLayout()
        files_grid.addWidget(self._mk_label("Gerber:"), 0, 0)
        files_grid.addWidget(self.gerber_edit, 0, 1)
        files_grid.addWidget(self.gerber_browse_btn, 0, 2)
        files_grid.addWidget(self._mk_label("Output PNG:"), 1, 0)
        files_grid.addWidget(self.png_edit, 1, 1)
        files_grid.addWidget(self.png_browse_btn, 1, 2)
        files_grid.setColumnStretch(1, 1)
        files_group = QGroupBox("Files"); files_group.setLayout(files_grid)

        # Display settings (shared)
        self.sb_disp_px_w = QSpinBox(); self._cfg_spin(self.sb_disp_px_w, 1, 200000)
        self.sb_disp_px_h = QSpinBox(); self._cfg_spin(self.sb_disp_px_h, 1, 200000)
        self.sb_disp_w_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_disp_w_mm)
        self.sb_disp_h_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_disp_h_mm)

        disp_grid = QGridLayout(); r = 0
        disp_grid.addWidget(self._mk_label("Display Width (px)"), r, 0)
        disp_grid.addWidget(self.sb_disp_px_w, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Height (px)"), r, 0)
        disp_grid.addWidget(self.sb_disp_px_h, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Width (mm)"), r, 0)
        disp_grid.addWidget(self.sb_disp_w_mm, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Height (mm)"), r, 0)
        disp_grid.addWidget(self.sb_disp_h_mm, r, 2); r += 1

        # PCB size
        self.sb_pcb_w_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_pcb_w_mm); self.sb_pcb_w_mm.setRange(0.001, 10000)
        self.sb_pcb_h_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_pcb_h_mm); self.sb_pcb_h_mm.setRange(0.001, 10000)
        disp_grid.addWidget(self._mk_label("PCB Width (mm)"), r, 0)
        disp_grid.addWidget(self.sb_pcb_w_mm, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("PCB Height (mm)"), r, 0)
        disp_grid.addWidget(self.sb_pcb_h_mm, r, 2); r += 1
        disp_grid.setColumnStretch(1, 1)

        # Options (real QCheckBox requested)
        self.chk_inv = QCheckBox("Invert")
        self.chk_mir = QCheckBox("Mirror (top layer)")
        self.chk_mir.setChecked(True)
        opt_row = QHBoxLayout(); opt_row.addWidget(self.chk_inv); opt_row.addWidget(self.chk_mir); opt_row.addStretch(1)

        disp_v = QVBoxLayout(); disp_v.addLayout(disp_grid); disp_v.addLayout(opt_row)
        disp_group = QGroupBox("Settings"); disp_group.setLayout(disp_v)

        # Buttons
        self.prepare_btn = QPushButton("Prepare Output")
        self.save_btn = QPushButton("Save PNG"); self.save_btn.setEnabled(False)
        self.prepare_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.save_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prepare_btn.clicked.connect(self.prepare_output)
        self.save_btn.clicked.connect(self.save_png)
        btn_row = QHBoxLayout(); btn_row.addWidget(self.prepare_btn); btn_row.addWidget(self.save_btn)

        # Status
        self.status_row = StatusRow()

        # Layout
        v = QVBoxLayout(self)
        v.addWidget(files_group)
        v.addWidget(disp_group)
        v.addLayout(btn_row)
        v.addWidget(self.status_row)
        v.addStretch(1)

        # model sync
        DISPLAY_MODEL.changed.connect(self._sync_from_model)
        self._sync_from_model()
        self.sb_disp_px_w.valueChanged.connect(self._on_display_changed)
        self.sb_disp_px_h.valueChanged.connect(self._on_display_changed)
        self.sb_disp_w_mm.valueChanged.connect(self._on_display_changed)
        self.sb_disp_h_mm.valueChanged.connect(self._on_display_changed)
        self.sb_pcb_w_mm.valueChanged.connect(self._on_pcb_changed)
        self.sb_pcb_h_mm.valueChanged.connect(self._on_pcb_changed)

        self.load_settings()
        self.set_status("Ready.", "ok")

    # helpers
    def _mk_label(self, text):
        lbl = QLabel(text); lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); return lbl
    def _cfg_spin(self, sb, mn, mx):
        sb.setRange(mn, mx); sb.setAlignment(Qt.AlignRight); sb.setFixedWidth(150)
    def _cfg_dspin(self, dsb):
        dsb.setDecimals(3); dsb.setRange(0.001, 10000); dsb.setAlignment(Qt.AlignRight); dsb.setFixedWidth(150)

    def set_status(self, text, state="ok"):
        self.status_row.set_status(text, state)

    def _set_controls_enabled(self, en: bool):
        self.gerber_edit.setEnabled(en); self.gerber_browse_btn.setEnabled(en)
        self.png_edit.setEnabled(en); self.png_browse_btn.setEnabled(en)
        self.sb_disp_px_w.setEnabled(en); self.sb_disp_px_h.setEnabled(en)
        self.sb_disp_w_mm.setEnabled(en); self.sb_disp_h_mm.setEnabled(en)
        self.sb_pcb_w_mm.setEnabled(en); self.sb_pcb_h_mm.setEnabled(en)
        self.chk_inv.setEnabled(en); self.chk_mir.setEnabled(en)
        self.prepare_btn.setEnabled(en)

    # settings
    def load_settings(self):
        s = self.settings
        path = s.value("gerber/path", "", type=str)
        outp = s.value("gerber/output", "", type=str)
        inv  = s.value("gerber/invert", False, type=bool)
        mir  = s.value("gerber/mirror", True, type=bool)
        pcbw = s.value("gerber/pcb_w_mm", self._pcb_w_mm_default, type=float)
        pcbh = s.value("gerber/pcb_h_mm", self._pcb_h_mm_default, type=float)
        self.gerber_edit.setText(path); self.png_edit.setText(outp)
        self.chk_inv.setChecked(inv); self.chk_mir.setChecked(mir)
        self.sb_pcb_w_mm.setValue(pcbw); self.sb_pcb_h_mm.setValue(pcbh)
        if path and os.path.isfile(path):
            self.gerber_path = path

    def save_settings(self):
        s = self.settings
        s.setValue("gerber/path", self.gerber_edit.text().strip())
        s.setValue("gerber/output", self.png_edit.text().strip())
        s.setValue("gerber/invert", self.chk_inv.isChecked())
        s.setValue("gerber/mirror", self.chk_mir.isChecked())
        s.setValue("gerber/pcb_w_mm", self.sb_pcb_w_mm.value())
        s.setValue("gerber/pcb_h_mm", self.sb_pcb_h_mm.value())
        s.sync()

    # model sync
    def _sync_from_model(self):
        self.sb_disp_px_w.blockSignals(True); self.sb_disp_px_h.blockSignals(True)
        self.sb_disp_w_mm.blockSignals(True); self.sb_disp_h_mm.blockSignals(True)
        self.sb_disp_px_w.setValue(DISPLAY_MODEL.pix_w)
        self.sb_disp_px_h.setValue(DISPLAY_MODEL.pix_h)
        self.sb_disp_w_mm.setValue(DISPLAY_MODEL.mm_w)
        self.sb_disp_h_mm.setValue(DISPLAY_MODEL.mm_h)
        self.sb_disp_px_w.blockSignals(False); self.sb_disp_px_h.blockSignals(False)
        self.sb_disp_w_mm.blockSignals(False); self.sb_disp_h_mm.blockSignals(False)

    def _on_display_changed(self):
        DISPLAY_MODEL.set_values(
            self.sb_disp_px_w.value(),
            self.sb_disp_px_h.value(),
            float(self.sb_disp_w_mm.value()),
            float(self.sb_disp_h_mm.value()),
        )
        self.save_settings()

    def _on_pcb_changed(self):
        self.save_settings()

    # browse
    def browse_gerber(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Select Gerber file", "", "Gerber (*.gbr *.gtl *.gbl)"
        )
        if fn:
            self.gerber_edit.setText(fn); self.gerber_path = fn
            self.set_status("Gerber file selected (not processed yet).", "ok")
            self.save_settings()

    def browse_png(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Select output PNG", "out.png", "PNG (*.png)")
        if fn:
            if not fn.lower().endswith(".png"): fn += ".png"
            self.png_edit.setText(fn)
            self.set_status("Output path set.", "ok")
            self.save_settings()

    # prepare / save
    def prepare_output(self):
        if self._prepare_thread and self._prepare_thread.isRunning():
            self.set_status("Already preparing…", "busy"); return
        if not self.gerber_edit.text().strip():
            self.set_status("No Gerber file selected.", "error"); return
        if not os.path.isfile(self.gerber_edit.text().strip()):
            self.set_status("Gerber file not found.", "error"); return
        if not HAVE_PYGERBER:
            self.set_status("pygerber not installed.", "error"); return

        self.set_status("Preparing…", "busy")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._set_controls_enabled(False); self.save_btn.setEnabled(False)

        self._prepare_worker = GerberWorker(
            path=self.gerber_edit.text().strip(),
            invert=self.chk_inv.isChecked(),
            mirror=self.chk_mir.isChecked(),
            disp_pix_w=DISPLAY_MODEL.pix_w,
            disp_pix_h=DISPLAY_MODEL.pix_h,
            disp_mm_w=DISPLAY_MODEL.mm_w,
            disp_mm_h=DISPLAY_MODEL.mm_h,
            pcb_mm_w=float(self.sb_pcb_w_mm.value()),
            pcb_mm_h=float(self.sb_pcb_h_mm.value()),
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
        self._prepare_thread = None; self._prepare_worker = None

    def _prepare_error(self, msg: str):
        print("Gerber render error:", msg)
        self.set_status("Render error (see console).", "error")
        self.save_btn.setEnabled(False)

    def _prepare_finished(self, pil_img: Image.Image):
        self.image = pil_img
        if not self.png_edit.text().strip() and self.gerber_edit.text().strip():
            base = os.path.splitext(os.path.basename(self.gerber_edit.text().strip()))[0] + ".png"
            out_path = os.path.join(os.path.dirname(self.gerber_edit.text().strip()), base)
            self.png_edit.setText(out_path)
        self.preview_callback(self.image)
        self.set_status("Output prepared. Click 'Save PNG' to write file.", "ok")
        self.save_btn.setEnabled(True)
        self.save_settings()

    def save_png(self):
        if self.image is None:
            self.set_status("Nothing to save – click 'Prepare Output' first.", "error"); return
        fn = self.png_edit.text().strip()
        if not fn:
            fn, _ = QFileDialog.getSaveFileName(self, "Save PNG", "out.png", "PNG (*.png)")
            if not fn: return
            if not fn.lower().endswith(".png"): fn += ".png"
        else:
            if not fn.lower().endswith(".png"): fn += ".png"
        try:
            self.image.save(fn, format="PNG")
            self.set_status(f"Saved: {os.path.basename(fn)}", "ok")
            self.save_settings()
        except Exception as e:
            print("Gerber save error:", e)
            self.set_status("Save error (see console).", "error")


# ==================================================================
# ----------------------- GDS TAB ----------------------------------
# ==================================================================
CIRCLE_DIAM_MM_GDS   = 100.0
CIRCLE_OFFSET_MM_GDS = 60.0


def _gds_render_to_photomask(
    gds_path: str,
    cell_name: str,
    layer: int,
    disp_pix_w: int,
    disp_pix_h: int,
    disp_mm_w: float,
    disp_mm_h: float,
    circle_diam_mm: float = CIRCLE_DIAM_MM_GDS,
    circle_offset_mm: float = CIRCLE_OFFSET_MM_GDS,
) -> Image.Image:

    lib = gdspy.GdsLibrary(infile=gds_path)
    if cell_name not in lib.cells:
        raise ValueError(f"Cell '{cell_name}' not found in GDS.")
    original_cell = lib.cells[cell_name]
    temp_name = f"__temp_flat__{uuid.uuid4().hex}"
    flat_cell = original_cell.copy(name=temp_name)
    flat_cell.flatten()
    polys = flat_cell.get_polygons(by_spec=True).get((layer, 0), [])
    if not polys:
        if temp_name in lib.cells:
            del lib.cells[temp_name]
        raise ValueError(f"No geometry on layer {layer}.")

    px_per_mm_x = disp_pix_w / disp_mm_w
    px_per_mm_y = disp_pix_h / disp_mm_h

    radius_px_x = int((circle_diam_mm / 2.0) * px_per_mm_x)
    radius_px_y = int((circle_diam_mm / 2.0) * px_per_mm_y)

    scale_um_to_px_x = px_per_mm_x / 1000.0
    scale_um_to_px_y = px_per_mm_y / 1000.0

    bbox = flat_cell.get_bounding_box()
    if bbox is None:
        if temp_name in lib.cells:
            del lib.cells[temp_name]
        raise ValueError("Empty cell; no bounding box.")

    cx_um = (bbox[0][0] + bbox[1][0]) / 2.0
    cy_um = (bbox[0][1] + bbox[1][1]) / 2.0

    gds_img = Image.new("L", (2 * radius_px_x, 2 * radius_px_y), 0)
    draw = ImageDraw.Draw(gds_img)
    for poly in polys:
        pts = []
        for x, y in poly:
            xs = (x - cx_um) * scale_um_to_px_x + radius_px_x
            ys = (cy_um - y) * scale_um_to_px_y + radius_px_y
            pts.append((xs, ys))
        draw.polygon(pts, fill=255)

    base = Image.new("L", (disp_pix_w, disp_pix_h), 0)

    _gds_paste_circle(
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
    _gds_paste_circle(
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

    base = ImageOps.mirror(base)

    if temp_name in lib.cells:
        del lib.cells[temp_name]
    return base


def _gds_paste_circle(base, img, rx, ry, px_mm_x, px_mm_y, pos, offset_mm, invert=False):
    if invert:
        img = ImageOps.invert(img)
    cy = base.height // 2
    if pos == "left":
        cx = int(offset_mm * px_mm_x)
    else:
        disp_mm_w = base.width / px_mm_x
        cx = int((disp_mm_w - offset_mm) * px_mm_x)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, *img.size], fill=255)
    base.paste(img, (cx - rx, cy - ry), mask)


class GDSWorker(QObject):
    finished = pyqtSignal(Image.Image)
    error = pyqtSignal(str)

    def __init__(self, gds_path: str, cell_name: str, layer: int,
                 disp_pix_w: int, disp_pix_h: int, disp_mm_w: float, disp_mm_h: float):
        super().__init__()
        self.gds_path = gds_path
        self.cell_name = cell_name
        self.layer = layer
        self.disp_pix_w = disp_pix_w
        self.disp_pix_h = disp_pix_h
        self.disp_mm_w = disp_mm_w
        self.disp_mm_h = disp_mm_h

    def run(self):
        try:
            img = _gds_render_to_photomask(
                gds_path=self.gds_path,
                cell_name=self.cell_name,
                layer=self.layer,
                disp_pix_w=self.disp_pix_w,
                disp_pix_h=self.disp_pix_h,
                disp_mm_w=self.disp_mm_w,
                disp_mm_h=self.disp_mm_h,
            )
        except Exception as e:
            self.error.emit(str(e))
            return
        self.finished.emit(img)


class GDSTab(QWidget):
    def __init__(self, settings: QSettings, preview_callback, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.preview_callback = preview_callback

        self.image: Optional[Image.Image] = None
        self.gds_path: Optional[str] = None
        self.selected_cell: Optional[str] = None
        self.selected_layer: Optional[int] = None

        self._prepare_thread = None
        self._prepare_worker = None

        # Files
        self.gds_edit = QLineEdit(); self.gds_edit.setPlaceholderText("Select GDS file…")
        self.gds_browse_btn = QPushButton("Browse..."); self.gds_browse_btn.clicked.connect(self.browse_gds)
        self.png_edit = QLineEdit(); self.png_edit.setPlaceholderText("Select output PNG…")
        self.png_browse_btn = QPushButton("Browse..."); self.png_browse_btn.clicked.connect(self.browse_png)

        files_grid = QGridLayout()
        files_grid.addWidget(self._mk_label("GDS:"), 0, 0)
        files_grid.addWidget(self.gds_edit, 0, 1)
        files_grid.addWidget(self.gds_browse_btn, 0, 2)
        files_grid.addWidget(self._mk_label("Output PNG:"), 1, 0)
        files_grid.addWidget(self.png_edit, 1, 1)
        files_grid.addWidget(self.png_browse_btn, 1, 2)
        files_grid.setColumnStretch(1, 1)
        files_group = QGroupBox("Files"); files_group.setLayout(files_grid)

        # Display settings (shared)
        self.sb_disp_px_w = QSpinBox(); self._cfg_spin(self.sb_disp_px_w, 1, 200000)
        self.sb_disp_px_h = QSpinBox(); self._cfg_spin(self.sb_disp_px_h, 1, 200000)
        self.sb_disp_w_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_disp_w_mm)
        self.sb_disp_h_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_disp_h_mm)

        disp_grid = QGridLayout(); r = 0
        disp_grid.addWidget(self._mk_label("Display Width (px)"), r, 0)
        disp_grid.addWidget(self.sb_disp_px_w, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Height (px)"), r, 0)
        disp_grid.addWidget(self.sb_disp_px_h, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Width (mm)"), r, 0)
        disp_grid.addWidget(self.sb_disp_w_mm, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Height (mm)"), r, 0)
        disp_grid.addWidget(self.sb_disp_h_mm, r, 2); r += 1
        disp_grid.setColumnStretch(1, 1)
        disp_group = QGroupBox("Settings"); disp_group.setLayout(disp_grid)

        # Design Selection
        self.combo_cell = QComboBox(); self.combo_cell.setEnabled(False)
        self.combo_layer = QComboBox(); self.combo_layer.setEnabled(False)
        self.combo_cell.currentIndexChanged.connect(self._cell_changed)
        self.combo_layer.currentIndexChanged.connect(self._layer_changed)

        design_grid = QGridLayout()
        design_grid.addWidget(self._mk_label("Cell"), 0, 0)
        design_grid.addWidget(self.combo_cell, 0, 2)
        design_grid.addWidget(self._mk_label("Layer"), 1, 0)
        design_grid.addWidget(self.combo_layer, 1, 2)
        design_grid.setColumnStretch(1, 1)
        design_group = QGroupBox("Design Selection"); design_group.setLayout(design_grid)

        # Buttons
        self.prepare_btn = QPushButton("Prepare Output")
        self.save_btn = QPushButton("Save PNG"); self.save_btn.setEnabled(False)
        self.prepare_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.save_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prepare_btn.clicked.connect(self.prepare_output)
        self.save_btn.clicked.connect(self.save_png)
        btn_row = QHBoxLayout(); btn_row.addWidget(self.prepare_btn); btn_row.addWidget(self.save_btn)

        # Status
        self.status_row = StatusRow()

        # Layout
        v = QVBoxLayout(self)
        v.addWidget(files_group)
        v.addWidget(disp_group)
        v.addWidget(design_group)
        v.addLayout(btn_row)
        v.addWidget(self.status_row)
        v.addStretch(1)

        # model sync
        DISPLAY_MODEL.changed.connect(self._sync_from_model)
        self._sync_from_model()
        self.sb_disp_px_w.valueChanged.connect(self._on_display_changed)
        self.sb_disp_px_h.valueChanged.connect(self._on_display_changed)
        self.sb_disp_w_mm.valueChanged.connect(self._on_display_changed)
        self.sb_disp_h_mm.valueChanged.connect(self._on_display_changed)

        # settings load
        self.load_settings()
        self.set_status("Ready.", "ok")

    # helpers
    def _mk_label(self, text):
        lbl = QLabel(text); lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); return lbl
    def _cfg_spin(self, sb, mn, mx):
        sb.setRange(mn, mx); sb.setAlignment(Qt.AlignRight); sb.setFixedWidth(150)
    def _cfg_dspin(self, dsb):
        dsb.setDecimals(3); dsb.setRange(0.001, 10000); dsb.setAlignment(Qt.AlignRight); dsb.setFixedWidth(150)

    def set_status(self, text, state="ok"):
        self.status_row.set_status(text, state)

    def _set_controls_enabled(self, en):
        self.gds_edit.setEnabled(en); self.gds_browse_btn.setEnabled(en)
        self.png_edit.setEnabled(en); self.png_browse_btn.setEnabled(en)
        self.sb_disp_px_w.setEnabled(en); self.sb_disp_px_h.setEnabled(en)
        self.sb_disp_w_mm.setEnabled(en); self.sb_disp_h_mm.setEnabled(en)
        self.combo_cell.setEnabled(en and self.combo_cell.count() > 0)
        self.combo_layer.setEnabled(en and self.combo_layer.count() > 0)
        self.prepare_btn.setEnabled(en)

    # settings
    def load_settings(self):
        s = self.settings
        gpath = s.value("gds/path", "", type=str)
        opath = s.value("gds/output", "", type=str)
        cell  = s.value("gds/cell", "", type=str)
        layer = s.value("gds/layer", "", type=str)
        self.gds_edit.setText(gpath)
        self.png_edit.setText(opath)
        if gpath and os.path.isfile(gpath):
            self.gds_path = gpath
            self._load_gds_metadata(gpath, cell, layer)

    def save_settings(self):
        s = self.settings
        s.setValue("gds/path", self.gds_edit.text().strip())
        s.setValue("gds/output", self.png_edit.text().strip())
        s.setValue("gds/cell", self.selected_cell if self.selected_cell else "")
        s.setValue("gds/layer", self.selected_layer if self.selected_layer is not None else "")
        s.sync()

    # model sync
    def _sync_from_model(self):
        self.sb_disp_px_w.blockSignals(True); self.sb_disp_px_h.blockSignals(True)
        self.sb_disp_w_mm.blockSignals(True); self.sb_disp_h_mm.blockSignals(True)
        self.sb_disp_px_w.setValue(DISPLAY_MODEL.pix_w)
        self.sb_disp_px_h.setValue(DISPLAY_MODEL.pix_h)
        self.sb_disp_w_mm.setValue(DISPLAY_MODEL.mm_w)
        self.sb_disp_h_mm.setValue(DISPLAY_MODEL.mm_h)
        self.sb_disp_px_w.blockSignals(False); self.sb_disp_px_h.blockSignals(False)
        self.sb_disp_w_mm.blockSignals(False); self.sb_disp_h_mm.blockSignals(False)

    def _on_display_changed(self):
        DISPLAY_MODEL.set_values(
            self.sb_disp_px_w.value(),
            self.sb_disp_px_h.value(),
            float(self.sb_disp_w_mm.value()),
            float(self.sb_disp_h_mm.value()),
        )
        self.save_settings()

    # combos
    def _cell_changed(self, idx):
        self.selected_cell = self.combo_cell.currentText(); self.save_settings()
    def _layer_changed(self, idx):
        try: self.selected_layer = int(self.combo_layer.currentText())
        except ValueError: self.selected_layer = None
        self.save_settings()

    def _load_gds_metadata(self, path: str, prefer_cell: str = "", prefer_layer: str = ""):
        try:
            lib = gdspy.GdsLibrary(infile=path)
        except Exception as e:
            self.set_status(f"Failed to load GDS: {e}", "error")
            self.combo_cell.clear(); self.combo_layer.clear()
            self.combo_cell.setEnabled(False); self.combo_layer.setEnabled(False)
            return

        self.combo_cell.blockSignals(True); self.combo_layer.blockSignals(True)
        self.combo_cell.clear(); self.combo_layer.clear()

        names = list(lib.cells.keys())
        for nm in names: self.combo_cell.addItem(nm)

        layers = set()
        for cell in lib.cells.values():
            for (ly, dt), _ in cell.get_polygons(by_spec=True).items():
                layers.add(ly)
        for ly in sorted(layers): self.combo_layer.addItem(str(ly))

        self.combo_cell.blockSignals(False); self.combo_layer.blockSignals(False)
        self.combo_cell.setEnabled(self.combo_cell.count() > 0)
        self.combo_layer.setEnabled(self.combo_layer.count() > 0)

        self.gds_path = path

        if prefer_cell and prefer_cell in names:
            i = self.combo_cell.findText(prefer_cell)
            if i >= 0: self.combo_cell.setCurrentIndex(i)
        if prefer_layer:
            i = self.combo_layer.findText(str(prefer_layer))
            if i >= 0: self.combo_layer.setCurrentIndex(i)

        self.selected_cell = self.combo_cell.currentText() if self.combo_cell.count() else None
        try: self.selected_layer = int(self.combo_layer.currentText()) if self.combo_layer.count() else None
        except ValueError: self.selected_layer = None

    # browse
    def browse_gds(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Select GDS file", "", "GDSII (*.gds *.gds2)")
        if fn:
            self.gds_edit.setText(fn); self.gds_path = fn
            self._load_gds_metadata(fn)
            self.set_status("GDS file loaded (not processed yet).", "ok")
            self.save_settings()

    def browse_png(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Select output PNG", "out.png", "PNG (*.png)")
        if fn:
            if not fn.lower().endswith(".png"): fn += ".png"
            self.png_edit.setText(fn)
            self.set_status("Output path set.", "ok")
            self.save_settings()

    # prepare
    def prepare_output(self):
        if self._prepare_thread and self._prepare_thread.isRunning():
            self.set_status("Already preparing…", "busy"); return
        if not self.gds_edit.text().strip():
            self.set_status("No GDS file selected.", "error"); return
        if not os.path.isfile(self.gds_edit.text().strip()):
            self.set_status("GDS file not found.", "error"); return
        if self.combo_cell.count() == 0 or self.combo_layer.count() == 0:
            self.set_status("No cell/layer available.", "error"); return
        cell_name = self.combo_cell.currentText()
        try: layer = int(self.combo_layer.currentText())
        except ValueError:
            self.set_status("Invalid layer selection.", "error"); return

        self.set_status("Preparing…", "busy")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._set_controls_enabled(False); self.save_btn.setEnabled(False)

        self._prepare_worker = GDSWorker(
            gds_path=self.gds_edit.text().strip(),
            cell_name=cell_name,
            layer=layer,
            disp_pix_w=DISPLAY_MODEL.pix_w,
            disp_pix_h=DISPLAY_MODEL.pix_h,
            disp_mm_w=DISPLAY_MODEL.mm_w,
            disp_mm_h=DISPLAY_MODEL.mm_h,
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
        self._prepare_thread = None; self._prepare_worker = None

    def _prepare_error(self, msg: str):
        print("GDS render error:", msg)
        self.set_status("Render error (see console).", "error")
        self.save_btn.setEnabled(False)

    def _prepare_finished(self, pil_img: Image.Image):
        self.image = pil_img
        if not self.png_edit.text().strip() and self.gds_edit.text().strip():
            base = os.path.splitext(os.path.basename(self.gds_edit.text().strip()))[0] + ".png"
            out_path = os.path.join(os.path.dirname(self.gds_edit.text().strip()), base)
            self.png_edit.setText(out_path)
        self.preview_callback(self.image)
        self.set_status("Output prepared. Click 'Save PNG' to write file.", "ok")
        self.save_btn.setEnabled(True)
        self.save_settings()

    def save_png(self):
        if self.image is None:
            self.set_status("Nothing to save – click 'Prepare Output' first.", "error"); return
        fn = self.png_edit.text().strip()
        if not fn:
            fn, _ = QFileDialog.getSaveFileName(self, "Save PNG", "out.png", "PNG (*.png)")
            if not fn: return
            if not fn.lower().endswith(".png"): fn += ".png"
        else:
            if not fn.lower().endswith(".png"): fn += ".png"
        try:
            self.image.save(fn, format="PNG")
            self.set_status(f"Saved: {os.path.basename(fn)}", "ok")
            self.save_settings()
        except Exception as e:
            print("GDS save error:", e)
            self.set_status("Save error (see console).", "error")


# ==================================================================
# ----------------------- BITMAP TAB -------------------------------
# ==================================================================
CIRCLE_DIAM_MM_BMP   = 100.0
CIRCLE_OFFSET_MM_BMP = 60.0


def _bmp_render_to_photomask(
    input_path: str,
    threshold: int,
    disp_pix_w: int,
    disp_pix_h: int,
    disp_mm_w: float,
    disp_mm_h: float,
    circle_diam_mm: float = CIRCLE_DIAM_MM_BMP,
    circle_offset_mm: float = CIRCLE_OFFSET_MM_BMP,
) -> Image.Image:
    px_per_mm_x = disp_pix_w / disp_mm_w
    px_per_mm_y = disp_pix_h / disp_mm_h

    radius_px_x = int((circle_diam_mm / 2.0) * px_per_mm_x)
    radius_px_y = int((circle_diam_mm / 2.0) * px_per_mm_y)

    center_y = disp_pix_h // 2
    left_center_x = int(circle_offset_mm * px_per_mm_x)
    right_center_x = int((disp_mm_w - circle_offset_mm) * px_per_mm_x)

    base = Image.new("L", (disp_pix_w, disp_pix_h), 0)
    img = Image.open(input_path)

    _bmp_place_in_circle(
        base_img=base,
        content_img=img,
        center=(left_center_x, center_y),
        radius_px_x=radius_px_x,
        radius_px_y=radius_px_y,
        threshold=threshold,
        invert=False,
    )
    _bmp_place_in_circle(
        base_img=base,
        content_img=img,
        center=(right_center_x, center_y),
        radius_px_x=radius_px_x,
        radius_px_y=radius_px_y,
        threshold=threshold,
        invert=True,
    )
    base = ImageOps.mirror(base)
    return base


def _bmp_place_in_circle(base_img, content_img, center, radius_px_x, radius_px_y, threshold=None, invert=False):
    content_img = content_img.convert("L")
    if threshold is not None:
        thr = int(threshold)
        content_img = content_img.point(lambda x: 255 if x >= thr else 0, mode='L')
    if invert:
        content_img = ImageOps.invert(content_img)
    min_side = min(content_img.size)
    left = (content_img.width  - min_side) // 2
    top  = (content_img.height - min_side) // 2
    content_cropped = content_img.crop((left, top, left + min_side, top + min_side))
    scaled = content_cropped.resize((2 * radius_px_x, 2 * radius_px_y), resample=RES_LANCZOS)
    mask = Image.new("L", (2 * radius_px_x, 2 * radius_px_y), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * radius_px_x, 2 * radius_px_y], fill=255)
    paste_position = (center[0] - radius_px_x, center[1] - radius_px_y)
    base_img.paste(scaled, paste_position, mask)


class BitmapWorker(QObject):
    finished = pyqtSignal(Image.Image)
    error = pyqtSignal(str)

    def __init__(self, input_path: str, threshold: int,
                 disp_pix_w: int, disp_pix_h: int,
                 disp_mm_w: float, disp_mm_h: float):
        super().__init__()
        self.input_path = input_path
        self.threshold = threshold
        self.disp_pix_w = disp_pix_w
        self.disp_pix_h = disp_pix_h
        self.disp_mm_w = disp_mm_w
        self.disp_mm_h = disp_mm_h

    def run(self):
        try:
            img = _bmp_render_to_photomask(
                input_path=self.input_path,
                threshold=self.threshold,
                disp_pix_w=self.disp_pix_w,
                disp_pix_h=self.disp_pix_h,
                disp_mm_w=self.disp_mm_w,
                disp_mm_h=self.disp_mm_h,
            )
        except Exception as e:
            self.error.emit(str(e))
            return
        self.finished.emit(img)


class BitmapTab(QWidget):
    """Bitmap tool tab with live-threshold preview."""
    def __init__(self, settings: QSettings, preview_callback, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.preview_callback = preview_callback

        self.image: Optional[Image.Image] = None
        self.image_path: Optional[str] = None

        self._prepare_thread = None
        self._prepare_worker = None
        self._live_preview_timer = QTimer(self)
        self._live_preview_timer.setSingleShot(True)
        self._live_preview_timer.setInterval(200)  # debounce ms
        self._live_preview_timer.timeout.connect(self._live_preview_trigger)

        # Files
        self.bmp_edit = QLineEdit(); self.bmp_edit.setPlaceholderText("Select bitmap…")
        self.bmp_browse_btn = QPushButton("Browse..."); self.bmp_browse_btn.clicked.connect(self.browse_bitmap)
        self.png_edit = QLineEdit(); self.png_edit.setPlaceholderText("Select output PNG…")
        self.png_browse_btn = QPushButton("Browse..."); self.png_browse_btn.clicked.connect(self.browse_png)

        files_grid = QGridLayout()
        files_grid.addWidget(self._mk_label("Bitmap:"), 0, 0)
        files_grid.addWidget(self.bmp_edit, 0, 1)
        files_grid.addWidget(self.bmp_browse_btn, 0, 2)
        files_grid.addWidget(self._mk_label("Output PNG:"), 1, 0)
        files_grid.addWidget(self.png_edit, 1, 1)
        files_grid.addWidget(self.png_browse_btn, 1, 2)
        files_grid.setColumnStretch(1, 1)
        files_group = QGroupBox("Files"); files_group.setLayout(files_grid)

        # Display settings (shared)
        self.sb_disp_px_w = QSpinBox(); self._cfg_spin(self.sb_disp_px_w, 1, 200000)
        self.sb_disp_px_h = QSpinBox(); self._cfg_spin(self.sb_disp_px_h, 1, 200000)
        self.sb_disp_w_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_disp_w_mm)
        self.sb_disp_h_mm = QDoubleSpinBox(); self._cfg_dspin(self.sb_disp_h_mm)

        disp_grid = QGridLayout(); r = 0
        disp_grid.addWidget(self._mk_label("Display Width (px)"), r, 0)
        disp_grid.addWidget(self.sb_disp_px_w, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Height (px)"), r, 0)
        disp_grid.addWidget(self.sb_disp_px_h, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Width (mm)"), r, 0)
        disp_grid.addWidget(self.sb_disp_w_mm, r, 2); r += 1
        disp_grid.addWidget(self._mk_label("Display Height (mm)"), r, 0)
        disp_grid.addWidget(self.sb_disp_h_mm, r, 2); r += 1
        disp_grid.setColumnStretch(1, 1)
        disp_group = QGroupBox("Settings"); disp_group.setLayout(disp_grid)

        # Threshold
        self.spin_threshold = QSpinBox(); self.spin_threshold.setRange(0, 254); self.spin_threshold.setAlignment(Qt.AlignRight); self.spin_threshold.setFixedWidth(150)
        self.spin_threshold.valueChanged.connect(self._on_threshold_changed)
        thresh_grid = QGridLayout()
        thresh_grid.addWidget(self._mk_label("Threshold (0–254)"), 0, 0)
        thresh_grid.addWidget(self.spin_threshold, 0, 2)
        thresh_grid.setColumnStretch(1, 1)
        thresh_group = QGroupBox("Threshold"); thresh_group.setLayout(thresh_grid)

        # Buttons
        self.prepare_btn = QPushButton("Prepare Output")
        self.save_btn = QPushButton("Save PNG"); self.save_btn.setEnabled(False)
        self.prepare_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.save_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prepare_btn.clicked.connect(self.prepare_output)
        self.save_btn.clicked.connect(self.save_png)
        btn_row = QHBoxLayout(); btn_row.addWidget(self.prepare_btn); btn_row.addWidget(self.save_btn)

        # Status
        self.status_row = StatusRow()

        # Layout
        v = QVBoxLayout(self)
        v.addWidget(files_group)
        v.addWidget(disp_group)
        v.addWidget(thresh_group)
        v.addLayout(btn_row)
        v.addWidget(self.status_row)
        v.addStretch(1)

        # model sync
        DISPLAY_MODEL.changed.connect(self._sync_from_model)
        self._sync_from_model()
        self.sb_disp_px_w.valueChanged.connect(self._on_display_changed)
        self.sb_disp_px_h.valueChanged.connect(self._on_display_changed)
        self.sb_disp_w_mm.valueChanged.connect(self._on_display_changed)
        self.sb_disp_h_mm.valueChanged.connect(self._on_display_changed)

        self.load_settings()
        self.set_status("Ready.", "ok")

    # helpers
    def _mk_label(self, text):
        lbl = QLabel(text); lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); return lbl
    def _cfg_spin(self, sb, mn, mx):
        sb.setRange(mn, mx); sb.setAlignment(Qt.AlignRight); sb.setFixedWidth(150)
    def _cfg_dspin(self, dsb):
        dsb.setDecimals(3); dsb.setRange(0.001, 10000); dsb.setAlignment(Qt.AlignRight); dsb.setFixedWidth(150)

    def set_status(self, text, state="ok"):
        self.status_row.set_status(text, state)

    def _set_controls_enabled(self, en):
        self.bmp_edit.setEnabled(en); self.bmp_browse_btn.setEnabled(en)
        self.png_edit.setEnabled(en); self.png_browse_btn.setEnabled(en)
        self.sb_disp_px_w.setEnabled(en); self.sb_disp_px_h.setEnabled(en)
        self.sb_disp_w_mm.setEnabled(en); self.sb_disp_h_mm.setEnabled(en)
        self.spin_threshold.setEnabled(en); self.prepare_btn.setEnabled(en)

    # settings
    def load_settings(self):
        s = self.settings
        bpath = s.value("bitmap/path", "", type=str)
        opath = s.value("bitmap/output", "", type=str)
        thr   = s.value("bitmap/threshold", 128, type=int)
        self.bmp_edit.setText(bpath); self.png_edit.setText(opath); self.spin_threshold.setValue(thr)
        if bpath and os.path.isfile(bpath): self.image_path = bpath

    def save_settings(self):
        s = self.settings
        s.setValue("bitmap/path", self.bmp_edit.text().strip())
        s.setValue("bitmap/output", self.png_edit.text().strip())
        s.setValue("bitmap/threshold", self.spin_threshold.value())
        s.sync()

    # model sync
    def _sync_from_model(self):
        self.sb_disp_px_w.blockSignals(True); self.sb_disp_px_h.blockSignals(True)
        self.sb_disp_w_mm.blockSignals(True); self.sb_disp_h_mm.blockSignals(True)
        self.sb_disp_px_w.setValue(DISPLAY_MODEL.pix_w)
        self.sb_disp_px_h.setValue(DISPLAY_MODEL.pix_h)
        self.sb_disp_w_mm.setValue(DISPLAY_MODEL.mm_w)
        self.sb_disp_h_mm.setValue(DISPLAY_MODEL.mm_h)
        self.sb_disp_px_w.blockSignals(False); self.sb_disp_px_h.blockSignals(False)
        self.sb_disp_w_mm.blockSignals(False); self.sb_disp_h_mm.blockSignals(False)

    def _on_display_changed(self):
        DISPLAY_MODEL.set_values(
            self.sb_disp_px_w.value(),
            self.sb_disp_px_h.value(),
            float(self.sb_disp_w_mm.value()),
            float(self.sb_disp_h_mm.value()),
        )
        self.save_settings()
        # (Optional) auto-preview on display change? currently off.

    # threshold -> debounce preview
    def _on_threshold_changed(self):
        self.save_settings()
        if self.image_path and os.path.isfile(self.image_path):
            self._live_preview_timer.start()

    def _live_preview_trigger(self):
        if self._prepare_thread and self._prepare_thread.isRunning():
            self._live_preview_timer.start(); return
        self.prepare_output(live_preview=True)

    # browse
    def browse_bitmap(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Select bitmap", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff)"
        )
        if fn:
            self.bmp_edit.setText(fn); self.image_path = fn
            self.set_status("Bitmap selected (not processed yet).", "ok")
            self.save_settings()

    def browse_png(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Select output PNG", "out.png", "PNG (*.png)")
        if fn:
            if not fn.lower().endswith(".png"): fn += ".png"
            self.png_edit.setText(fn)
            self.set_status("Output path set.", "ok")
            self.save_settings()

    # prepare
    def prepare_output(self, live_preview=False):
        if self._prepare_thread and self._prepare_thread.isRunning():
            self.set_status("Already preparing…", "busy"); return
        if not self.bmp_edit.text().strip():
            self.set_status("No bitmap selected.", "error"); return
        if not os.path.isfile(self.bmp_edit.text().strip()):
            self.set_status("Bitmap file not found.", "error"); return

        self.set_status("Preview updating…" if live_preview else "Preparing…", "busy")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._set_controls_enabled(False); self.save_btn.setEnabled(False)

        self._prepare_worker = BitmapWorker(
            input_path=self.bmp_edit.text().strip(),
            threshold=self.spin_threshold.value(),
            disp_pix_w=DISPLAY_MODEL.pix_w,
            disp_pix_h=DISPLAY_MODEL.pix_h,
            disp_mm_w=DISPLAY_MODEL.mm_w,
            disp_mm_h=DISPLAY_MODEL.mm_h,
        )
        self._prepare_thread = QThread(self)
        self._prepare_worker.moveToThread(self._prepare_thread)
        self._prepare_thread.started.connect(self._prepare_worker.run)
        self._prepare_worker.finished.connect(lambda img, live=live_preview: self._prepare_finished(img, live))
        self._prepare_worker.error.connect(self._prepare_error)
        self._prepare_worker.finished.connect(self._prepare_thread.quit)
        self._prepare_worker.error.connect(self._prepare_thread.quit)
        self._prepare_thread.finished.connect(self._prepare_worker.deleteLater)
        self._prepare_thread.finished.connect(self._thread_cleanup)
        self._prepare_thread.start()

    def _thread_cleanup(self):
        QApplication.restoreOverrideCursor()
        self._set_controls_enabled(True)
        self._prepare_thread = None; self._prepare_worker = None

    def _prepare_error(self, msg: str):
        print("Bitmap render error:", msg)
        self.set_status("Render error (see console).", "error")
        self.save_btn.setEnabled(False)

    def _prepare_finished(self, pil_img: Image.Image, live_preview=False):
        self.image = pil_img
        if not self.png_edit.text().strip() and self.bmp_edit.text().strip():
            base = os.path.splitext(os.path.basename(self.bmp_edit.text().strip()))[0] + ".png"
            out_path = os.path.join(os.path.dirname(self.bmp_edit.text().strip()), base)
            self.png_edit.setText(out_path)
        self.preview_callback(self.image)
        if live_preview:
            self.set_status(f"Preview updated (threshold={self.spin_threshold.value()}).", "ok")
        else:
            self.set_status("Output prepared. Click 'Save PNG' to write file.", "ok")
        self.save_btn.setEnabled(True)
        self.save_settings()

    def save_png(self):
        if self.image is None:
            self.set_status("Nothing to save – click 'Prepare Output' first.", "error"); return
        fn = self.png_edit.text().strip()
        if not fn:
            fn, _ = QFileDialog.getSaveFileName(self, "Save PNG", "out.png", "PNG (*.png)")
            if not fn: return
            if not fn.lower().endswith(".png"): fn += ".png"
        else:
            if not fn.lower().endswith(".png"): fn += ".png"
        try:
            self.image.save(fn, format="PNG")
            self.set_status(f"Saved: {os.path.basename(fn)}", "ok")
            self.save_settings()
        except Exception as e:
            print("Bitmap save error:", e)
            self.set_status("Save error (see console).", "error")


# ==================================================================
# Main Window hosting Tabs + Preview
# ==================================================================
class PhotomaskMain(QMainWindow):
    PREVIEW_W = 800
    PREVIEW_H = 600

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE_DISPLAY)

        self.settings = QSettings(COMPANY_NAME, APP_NAME_SETTINGS)

        # preview
        self.preview_view = PreviewView(self.PREVIEW_W, self.PREVIEW_H)

        # tabs
        self.tabs = QTabWidget()
        self.gerber_tab = GerberTab(self.settings, self.update_preview)
        self.gds_tab    = GDSTab(self.settings, self.update_preview)
        self.bmp_tab    = BitmapTab(self.settings, self.update_preview)
        self.tabs.addTab(self.gerber_tab, "Gerber")
        self.tabs.addTab(self.gds_tab,    "GDS")
        self.tabs.addTab(self.bmp_tab,    "Bitmap")

        # layout
        right_layout = QVBoxLayout(); right_layout.addWidget(self.preview_view, alignment=Qt.AlignTop | Qt.AlignLeft); right_layout.addStretch(1)
        right_widget = QWidget(); right_widget.setLayout(right_layout)
        main_layout = QHBoxLayout(); main_layout.addWidget(self.tabs); main_layout.addWidget(right_widget)
        container = QWidget(); container.setLayout(main_layout)
        self.setCentralWidget(container)

        # load global display settings to model
        self._load_global_display_settings()

    def closeEvent(self, event):
        self._save_global_display_settings()
        super().closeEvent(event)

    def update_preview(self, pil_img: Image.Image):
        pix = pil_to_qpixmap(pil_img)
        self.preview_view.set_image_pixmap(pix)

    def _load_global_display_settings(self):
        s = self.settings
        pix_w = s.value("display/pix_w", DISPLAY_MODEL.pix_w, type=int)
        pix_h = s.value("display/pix_h", DISPLAY_MODEL.pix_h, type=int)
        mm_w  = s.value("display/mm_w",  DISPLAY_MODEL.mm_w,  type=float)
        mm_h  = s.value("display/mm_h",  DISPLAY_MODEL.mm_h,  type=float)
        DISPLAY_MODEL.set_values(pix_w, pix_h, mm_w, mm_h)

    def _save_global_display_settings(self):
        s = self.settings
        s.setValue("display/pix_w", DISPLAY_MODEL.pix_w)
        s.setValue("display/pix_h", DISPLAY_MODEL.pix_h)
        s.setValue("display/mm_w",  DISPLAY_MODEL.mm_w)
        s.setValue("display/mm_h",  DISPLAY_MODEL.mm_h)
        s.sync()


# ==================================================================
# main()
# ==================================================================

def main():
    QCoreApplication.setOrganizationName(COMPANY_NAME)
    QCoreApplication.setApplicationName(APP_NAME_SETTINGS)

    app = QApplication(sys.argv)
    win = PhotomaskMain()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
