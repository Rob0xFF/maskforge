# LCD Photomask Toolkit

> **IMPORTANT WORKFLOW NOTE**\
> The PNG files generated by this toolkit are **intermediate photomasks**. To actually display them on your LCD exposure hardware, i.e. M-SLA 3D printer, you must pass them through **UVTools** (or an equivalent injection utility) that knows how to inject image frames to your specific printer files.\
> Without this step the images will *not* reach the LCD in a calibrated way.
>
> In a typical setup the standard resin vat of an MSLA printer is **removed** and replaced by a custom mechanical alignment frame that screws down over the LCD, accurately locating your substrate (PCB, wafer, etc.). The photomask PNGs produced here are meant to be shown on the LCD *under that frame.

**Generate high‑resolution LCD photomask PNGs from Gerber, GDSII, or bitmap sources — in one unified cross‑platform GUI.**

The Photomask Toolkit ("Maskforge Toolkit") targets DIY PCB exposure rigs, LCD photolithography setups, and experimental micro‑fabrication workflows. It lets you load design data from multiple EDA/file formats, map it into a calibrated LCD display space (pixel + physical mm), preview at pixel fidelity, and export production‑ready 1‑bit style masks (grayscale images with binary content).

![screenshot](Screenshot.png)

---

## Table of Contents

- [Features](#features)
- [Project Architecture](#project-architecture)
- [Supported Inputs](#supported-inputs)
  - [Gerber Tab](#gerber-tab)
  - [GDS Tab](#gds-tab)
  - [Bitmap Tab](#bitmap-tab)
- [Preview Panel](#preview-panel)
- [Shared Display Geometry Model](#shared-display-geometry-model)
- [Installation](#installation)
  - [Python Version](#python-version)
  - [Install via pip](#install-via-pip)
  - [Optional / Recommended Extras](#optional--recommended-extras)
- [Running](#running)
- [Using the Toolkit](#using-the-toolkit)
  - [1. Calibrate Your Display](#1-calibrate-your-display)
  - [2. Gerber Workflow](#2-gerber-workflow)
  - [3. GDS Workflow](#3-gds-workflow)
  - [4. Bitmap Workflow + Live Threshold Preview](#4-bitmap-workflow--live-threshold-preview)
- [File Output Details](#file-output-details)
- [Persisted Settings](#persisted-settings)
- [Pixel Fidelity Notes](#pixel-fidelity-notes)
- [Performance Tips](#performance-tips)
- [Packaging / Freezing (PyInstaller)](#packaging--freezing-pyinstaller)
- [Roadmap](#roadmap)
- [Known Issues / Limitations](#known-issues--limitations)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Acknowledgments & Libraries Used](#acknowledgments--libraries-used)
- [License](#license)

---

## Features

| Capability                          | Gerber                     | GDS          | Bitmap                          |
| ----------------------------------- | -------------------------- | ------------ | ------------------------------- |
| Load design / image                 | ✅                          | ✅            | ✅                               |
| User‑set LCD pixel dimensions       | ✅ (shared)                 | ✅ (shared)   | ✅ (shared)                      |
| User‑set LCD physical mm dims       | ✅ (shared)                 | ✅ (shared)   | ✅ (shared)                      |
| Design‑specific options             | PCB size + Invert + Mirror | Cell + Layer | Threshold (0‑254)               |
| Threaded background rendering       | ✅                          | ✅            | ✅                               |
| Pixel‑accurate preview              | ✅                          | ✅            | ✅                               |
| Live auto‑preview                   | Manual                     | Manual       | ✅ Debounced on threshold change |
| Persistent settings across sessions | ✅                          | ✅            | ✅                               |

Additional highlights:

- Massive display resolutions supported (default: 13,312 × 5,120 px).
- Physical mm calibration -> correct scaling from CAD to LCD.
- Dual exposure patterning (left/right regions) for GDS & Bitmap flows (one inverted exposure built in).
- Global final mirror where required to account for optical inversion paths.
- Hard‑pixel preview: no anti‑aliasing when viewing at 1:1 zoom; what you see is what the LCD gets.

---

## Project Architecture

The toolkit is structured as a **single PyQt5 main window** with:

- **Left**: A `QTabWidget` hosting three tool panels (Gerber, GDS, Bitmap). Each contains fields for file selection, display geometry, tool‑specific parameters, action buttons, and a status row with a colored LED indicator.
- **Right**: A shared zoomable **Preview** panel (`QGraphicsView`) that displays the last successfully rendered image from whichever tab you used most recently.
- **Asynchronous rendering**: Each render job runs in a `QThread` via tool‑specific worker objects to keep the UI responsive during large file processing.
- **Central settings model**: Display geometry values (pixels/mm) are shared across all tabs — change them once, everywhere updates.

---

## Supported Inputs

### Gerber Tab

Convert PCB copper/soldermask/etc. Gerber layer files into a calibrated LCD photomask.

**User Controls:**

- Gerber input file.
- Output PNG path.
- Display Width/Height (px + mm) — shared across tabs.
- PCB Width (mm) & PCB Height (mm) — area into which the Gerber is placed.
- *Invert* (optional polarity flip).
- *Mirror* (top‑layer prep, default ON).

**Pipeline:**

1. Gerber parsed via **pygerber**.
2. Render to high‑res grayscale buffer.
3. Reposition according to Gerber origin (min\_x / max\_y etc.).
4. Optional invert/mirror.
5. Center onto full LCD canvas.

### GDS Tab

Render a **GDSII** layout cell from a selected **layer** into **two circular exposure regions suitable for 4 inch / 100mm silicon wafers**, left & right, with the right region automatically inverted.

**User Controls:**

- GDS file.
- Output PNG path.
- Display geometry (shared).
- Cell selector.
- Layer selector.

**Pipeline:**

1. GDS loaded via **gdspy**.
2. Selected Cell is **flattened** (copy) to fully resolve references.
3. Polygons filtered by layer (datatype 0).
4. Bounding box used to center design inside circular mask area.
5. Two circular placements: left normal, right inverted.
6. Global mirror.

### Bitmap Tab

Import any bitmap (PNG/JPEG/etc.), threshold it to binary (>=T white, \<T black), then place into the same left/right circular layout used by the GDS pipeline (right inverted). Final global mirror.

**User Controls:**

- Bitmap file.
- Output PNG path.
- Display geometry (shared).
- **Threshold (0‑254)**.

**Live Threshold Preview:** Adjust the threshold spinbox and, after \~200 ms pause (debounce), the tool auto‑renders a new preview in the background. No need to press *Prepare Output* for visual tuning (you still can, if you want a deliberate run before saving).

---

## Preview Panel

The preview to the right is a `QGraphicsView` with:

- 800×600 fixed size viewport.
- Scroll/pan (drag with mouse).
- Mouse wheel zoom (bounded: 0.05× – 10×).
- Double‑click to re‑fit the full image.
- Anti‑aliasing disabled: **hard pixels** for accurate inspection.

Note that the preview scales the *rendered* photomask image — it displays the exact pixel data that will be saved.

---

## Shared Display Geometry Model

All three tabs reference a single `DisplayModel` object that stores:

- **Pixel Width & Height** (LCD native resolution)
- **Physical Width & Height** in millimeters

Changing these values in *any tab* propagates across the entire application. This ensures consistent scaling whether you're working from Gerber, GDS, or Bitmap sources in the same session.

> **Tip:** Save/quit/relaunch — your last geometry values reload automatically via QSettings.

---

## Installation

### Python Version

Tested with **Python 3.9+**. Should also work on 3.8–3.12 if all dependencies are available.

### Install via pip

Create and activate a virtual environment (recommended):

````bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --upgrade pip
``

Install required packages:

```bash
pip install pyqt5 pillow gdspy pygerber
````

If `pygerber` brings in optional extras you don't need (e.g., heavy parsing extensions), you can install minimal form:

```bash
pip install pygerber-core
```

…but you **must** have the modules used in the Gerber tab available at runtime.

### Optional / Recommended Extras

- `pyinstaller` – build a standalone app bundle.
- `numpy` – some libraries (gdspy) may benefit.
- `pyopengl` – for certain Qt backends (not required normally).

You can also capture dependencies in a file:

```txt
# requirements.txt
pyqt5>=5.15
pillow>=10
gdspy>=1.6
pygerber>=4
```

Install via:

```bash
pip install -r requirements.txt
```

---

## Running

From the project root:

```bash
python3 maskforge_toolkit.py
```

On macOS you may need to ensure the venv's Qt platform plugins are properly located; if you see plugin load errors, try:

```bash
export QT_DEBUG_PLUGINS=1
python3 maskforge_toolkit.py
```

---

## Using the Toolkit

### 1. Calibrate Your Display

Before generating masks, set the **Display Width / Height in pixels** and the **physical millimeter dimensions** of the active LCD panel:

1. Measure the active (lit) region of your LCD in mm (width × height).
2. Enter the LCD’s native resolution in pixels.
3. These values drive accurate px/mm scale factors used in all tabs.

---

### 2. Gerber Workflow

1. Open the **Gerber** tab.
2. Click **Browse…** next to *Gerber* and select a `.gbr`, `.gtl`, `.gbl`, etc. file.
3. Set an *Output PNG* path (optional; auto‑suggests on first prepare).
4. Verify **Display geometry** values.
5. Enter **PCB Width / Height (mm)** to define the active Gerber placement region.
6. Optional: enable **Invert** or **Mirror (top layer)**.
7. Click **Prepare Output**. A progress (Busy/Blue) indicator shows while rendering.
8. When complete (Green), preview updates. Inspect via zoom/pan.
9. Click **Save PNG** to export.

---

### 3. GDS Workflow

1. Open the **GDS** tab.
2. Select a GDSII file.
3. After load, choose a **Cell** and **Layer** (datatype 0 assumed per original script).
4. Confirm display geometry.
5. Click **Prepare Output**.
6. Preview shows dual circular exposures: left normal, right inverted; full image mirrored.
7. Save PNG.

> Large hierarchical GDS files are flattened (copy) per run; big IC layouts can take time and RAM.

---

### 4. Bitmap Workflow + Live Threshold Preview

1. Open the **Bitmap** tab.
2. Select any source image (PNG/JPEG/etc.).
3. Adjust **Threshold (0–254)** — values ≥ threshold become white (opaque); below become black.
4. Stop moving the control — after \~0.2 s the toolkit auto‑renders a **live preview**.
5. Use *Prepare Output* if you want to force a render (not required for live preview).
6. Save PNG when happy.

Left exposure = normal, Right exposure = inverted, then global mirror.

---

## File Output Details

- Output files are always **PNG** grayscale (mode "L").
- White = exposed (light pass) vs black = blocked — *interpretation depends on your optical stack*. You can flip via Invert (Gerber) or by inverting your exposure application.
- Default image size = user‑set **Display Width × Display Height (px)**.
- For GDS & Bitmap flows: two circular regions (100 mm dia by default) placed at ±60 mm from edges (configurable in code; expose as UI later if needed).
- All final images may be globally mirrored to match optical path expectations.

---

## Persisted Settings

Settings are stored via **QSettings** under:

- Organization: `Rob0xFF`
- Application: `Photomask Toolkit`

Values saved include:

- Global display geometry (px/mm).
- Per‑tab file paths.
- Gerber: invert, mirror, PCB size.
- GDS: last cell & layer.
- Bitmap: last threshold.

This lets you quit/relaunch without re‑entering common settings.

---

## Pixel Fidelity Notes

The preview deliberately disables:

- SmoothPixmapTransform
- Anti‑aliasing

This ensures that at 1:1 zoom levels you see **exact pixel edges** — critical for photomask debugging. When zoomed down, the Qt viewport will reduce, but no smoothing filter is injected by the view; what artifacts you see are due to screen scaling, not post filtering.

If you require native pixel blow‑up inspection, zoom in (mouse wheel up) until individual pixels are visible.

---

## Performance Tips

- **Use a virtualenv** to isolate exact dependency versions.
- **SSD storage** helps with very large PNG export times at 13k × 5k.
- **Memory**: Large intermediate arrays (especially GDS flatten + raster) can consume gigabytes; close other tabs if low on RAM.

---

## Packaging / Freezing (PyInstaller)

You can produce a standalone binary/app like this:

```bash
pyinstaller \
  --name maskforge_toolkit \
  --onefile \
  --windowed \
  maskforge_toolkit.py
```

If Qt plugins fail to load in the frozen build, add:

```bash
  --add-data "$(python -c 'import PyQt5,os;import PyQt5.QtCore as QC;import sys;from PyQt5.QtCore import QLibraryInfo;print(QLibraryInfo.location(QLibraryInfo.PluginsPath))')":PyQt5/Qt/plugins
```

See PyInstaller + PyQt5 docs for platform‑specific bundling.

---

## Roadmap


Have a request? Open an issue!

---

## Known Issues / Limitations

- Very large GDS layouts may take significant time to flatten. Consider pre‑flattening externally if performance is critical.
- Output always monochrome (8‑bit grayscale). No direct multi‑level grayscale dithering.
- Layer datatype is currently assumed `0` in the GDS flow (matches your original script). Expand as needed.

---

## Troubleshooting

**Gerber render error: pygerber not installed**\
Install: `pip install pygerber` (or `pygerber-core` + required extras).

**GDS error: Cell named ****temp\_flat**** already present**\
Fixed in current version by using UUID‑suffixed temporary cells; upgrade if you see this.

**Preview stays gray / no image**\
Make sure you clicked *Prepare Output* (Gerber/GDS) or selected a bitmap + adjusted threshold (Bitmap).

**MemoryError during render**\
Reduce resolution, close other apps, or upgrade RAM. GDS flattening can spike.

**Wrong scaling on LCD**\
Verify px & mm values; also check that your display system isn't scaling input video.

---

## Contributing

Pull requests welcome! Please:

1. Fork and branch from `main`.
2. Use clear commit messages.
3. Keep render cores functionally unchanged unless a bugfix is clearly warranted.
4. Test all three tabs before submitting.

For bigger changes, open an issue first so we can coordinate.

---

## Acknowledgments & Libraries Used

This project stands on the shoulders of excellent open‑source work:

| Library                                                         | Purpose in Toolkit                       | License (at time of writing)         |
| --------------------------------------------------------------- | ---------------------------------------- | ------------------------------------ |
| [**PyQt5**](https://riverbankcomputing.com/software/pyqt/intro) | GUI framework                            | GPL / Commercial (bindings to Qt)    |
| [**Qt**](https://www.qt.io/)                                    | Cross‑platform GUI runtime               | LGPL / Commercial (varies by module) |
| [**Pillow**](https://python-pillow.org/)                        | Image I/O, pixel processing, compositing | PIL license (BSD‑style)              |
| [**pygerber**](https://pypi.org/project/pygerber/)              | Gerber parsing + rasterization           | MIT (check project)                  |
| [**gdspy**](https://pypi.org/project/gdspy/)                    | GDSII parsing & geometry flattening      | BSD‑style (check project)            |
| **Python**                                                      | The language ❤️                          | PSF License                          |

> **Please verify licenses yourself** before distributing binaries; some combination use cases (PyQt GPL vs commercial) may impose obligations depending on distribution model.

Special thanks to the community projects and maintainers who make EDA and computational fabrication more accessible.

---

## License

GPL

---

**Happy exposing!** If you build something cool (PCB printer? DIY mask aligner? wafer stepper with tablet LCD?), please share pics or links — we'd love to see the Toolkit in action.



---

## Integration with PCBTools & Custom Alignment Frame

This project generates *raw* photomask PNGs that are dimensionally aligned to your LCD panel (pixel + mm). To actually project them:

1. **Generate PNG(s) in the Photomask Toolkit** using the appropriate tab (Gerber / GDS / Bitmap).
2. **Launch [PCBTools]** (or your own compatible image injector) and load the generated PNG.
   - PCBTools is responsible for pushing the image to your LCD controller, applying any display‑specific formatting, tiling, or color depth conversion that your hardware requires.
   - If you are scripting, PCBTools can be called headless; see its docs.
3. **Mount your custom alignment frame** in place of the resin vat on your LCD printer / exposure station.
   - The Schablone is a rigid, screw‑down plate that registers your workpiece (PCB panel, wafer, photoresist slide) to the LCD pixel grid.
   - Ensure the mechanical offsets used to design the Schablone match the display geometry you entered in the Photomask Toolkit.
4. **Expose**: With the workpiece secured under the alignment frame, command PCBTools (or your control software) to display the injected PNG at native resolution, full screen, 1:1 pixels.

### Alignment Tips

- Always verify orientation (mirrored vs non‑mirrored) across Toolkit → PCBTools → LCD.
- Run a fiducial test pattern first; measure overlay accuracy.
- Lock display scaling: no OS/window manager scaling! Use kiosk/fullscreen raw frame push if possible.

### File Naming

For smooth handoff to PCBTools, consider these conventions:

```
<project>_<layer>_<rev>_<dpi>.png
board_topmask_r02_native.png
```

Include mm dimensions in the repo metadata (README / YAML), since binary pixel size is implicit once display is calibrated.

> **Reminder:** The Photomask Toolkit does *not* talk directly to your LCD hardware. PCBTools (or similar) is the bridge.

---


