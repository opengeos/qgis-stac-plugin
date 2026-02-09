"""
Time Slider Dock Widget for STAC Explorer Plugin

Provides temporal navigation for multi-temporal COG raster layers,
with slider controls, auto-play, and transport buttons.
"""

import logging
import os

from osgeo import gdal

_logger = logging.getLogger(__name__)

from qgis.PyQt.QtCore import Qt, QTimer, QThread, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QPushButton,
    QSpinBox,
    QCheckBox,
    QProgressBar,
    QGroupBox,
    QMessageBox,
)
from qgis.PyQt.QtGui import QFont
from qgis.core import (
    QgsRasterLayer,
    QgsProject,
    QgsRasterShader,
    QgsColorRampShader,
    QgsSingleBandPseudoColorRenderer,
    QgsMultiBandColorRenderer,
    QgsStyle,
)


class TimeSliderLoadWorker(QThread):
    """Worker thread that pre-fetches remote raster metadata via GDAL."""

    progress = pyqtSignal(int, int, str)  # current, total, layer_name
    finished = pyqtSignal(list)  # list of (cog_url, layer_name, item_dict, valid)

    def __init__(self, layer_specs):
        """Initialize the worker.

        Args:
            layer_specs: List of (cog_url, layer_name, item_dict) tuples.
        """
        super().__init__()
        self.layer_specs = layer_specs

    def run(self):
        """Pre-fetch remote raster metadata for each layer."""
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        results = []
        total = len(self.layer_specs)
        for i, (cog_url, layer_name, item_dict) in enumerate(self.layer_specs):
            self.progress.emit(i + 1, total, layer_name)
            try:
                ds = gdal.Open(f"/vsicurl/{cog_url}")
                valid = ds is not None
                if not valid:
                    _logger.warning("GDAL returned None for: %s", cog_url)
                ds = None
            except Exception as e:
                _logger.warning("GDAL error opening %s: %s", cog_url, e)
                valid = False
            results.append((cog_url, layer_name, item_dict, valid))
        self.finished.emit(results)


class TimeSliderDockWidget(QDockWidget):
    """Time slider dock widget for stepping through temporal raster layers."""

    def __init__(self, iface, parent=None):
        """Initialize the time slider dock.

        Args:
            iface: QGIS interface instance.
            parent: Parent widget.
        """
        super().__init__("STAC Explorer Time Slider", parent)
        self.iface = iface

        self._time_steps = []
        self._layer_group = None
        self._workers = []
        self._pending_asset_key = None
        self._auto_play_timer = QTimer(self)
        self._auto_play_timer.timeout.connect(self._auto_play_step)

        self.setAllowedAreas(Qt.TopDockWidgetArea | Qt.BottomDockWidgetArea)

        self._setup_ui()

    def _start_worker(self, worker):
        """Start a QThread worker and track it for cleanup.

        Args:
            worker: QThread worker to start.
        """

        def _cleanup():
            if worker in self._workers:
                self._workers.remove(worker)

        worker.finished.connect(_cleanup)
        self._workers.append(worker)
        worker.start()

    def _stop_all_workers(self):
        """Wait for all active workers to finish."""
        for worker in list(self._workers):
            worker.wait(5000)
        self._workers.clear()

    def closeEvent(self, event):
        """Handle dock close event, ensuring workers are stopped.

        Args:
            event: The close event.
        """
        self._stop_all_workers()
        super().closeEvent(event)

    def _setup_ui(self):
        """Set up the dock widget UI."""
        container = QWidget()
        layout = QVBoxLayout(container)

        # Current date label
        self.date_label = QLabel("No data loaded")
        date_font = QFont()
        date_font.setPointSize(14)
        date_font.setBold(True)
        self.date_label.setFont(date_font)
        self.date_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.date_label)

        # Step counter
        self.step_label = QLabel("")
        self.step_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.step_label)

        # Slider
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.slider)

        # Transport controls
        transport_layout = QHBoxLayout()

        self.first_btn = QPushButton("|<<")
        self.first_btn.setToolTip("First")
        self.first_btn.clicked.connect(self._go_first)
        self.first_btn.setEnabled(False)
        transport_layout.addWidget(self.first_btn)

        self.prev_btn = QPushButton("<<")
        self.prev_btn.setToolTip("Previous")
        self.prev_btn.clicked.connect(self._go_previous)
        self.prev_btn.setEnabled(False)
        transport_layout.addWidget(self.prev_btn)

        self.next_btn = QPushButton(">>")
        self.next_btn.setToolTip("Next")
        self.next_btn.clicked.connect(self._go_next)
        self.next_btn.setEnabled(False)
        transport_layout.addWidget(self.next_btn)

        self.last_btn = QPushButton(">>|")
        self.last_btn.setToolTip("Last")
        self.last_btn.clicked.connect(self._go_last)
        self.last_btn.setEnabled(False)
        transport_layout.addWidget(self.last_btn)

        layout.addLayout(transport_layout)

        # Auto-play controls
        play_group = QGroupBox("Auto-Play")
        play_layout = QHBoxLayout(play_group)

        self.auto_play_cb = QCheckBox("Enable")
        play_layout.addWidget(self.auto_play_cb)

        play_layout.addWidget(QLabel("Speed:"))
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(1, 30)
        self.speed_spin.setValue(2)
        self.speed_spin.setSuffix(" sec")
        play_layout.addWidget(self.speed_spin)

        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        self.play_btn.setEnabled(False)
        play_layout.addWidget(self.play_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_play)
        self.stop_btn.setEnabled(False)
        play_layout.addWidget(self.stop_btn)

        layout.addWidget(play_group)

        # Progress bar and status label (for loading)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        # Clear button
        self.clear_btn = QPushButton("Clear All Layers")
        self.clear_btn.clicked.connect(self.clear)
        self.clear_btn.setEnabled(False)
        layout.addWidget(self.clear_btn)

        self.setWidget(container)

    def _prepare_gdal_for_loading(self):
        """Configure GDAL for public COG loading.

        Sets timeouts, cache configuration, and anonymous S3 access
        required for public catalogs like Element84 Earth Search.
        """
        config = {
            "GDAL_HTTP_TIMEOUT": "30",
            "GDAL_HTTP_MAX_RETRY": "3",
            "GDAL_HTTP_RETRY_DELAY": "2",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "VSI_CACHE": "TRUE",
            "VSI_CACHE_SIZE": "200000000",
            "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
            "AWS_NO_SIGN_REQUEST": "YES",
        }
        for key, value in config.items():
            gdal.SetConfigOption(key, value)
            os.environ[key] = value

    def load_items(self, items, asset_key, render_settings=None):
        """Load STAC items as raster layers into a layer group.

        Pre-fetches metadata in a background thread, then creates layers
        on the main thread from the VSI cache.

        Args:
            items: List of item dicts from STACBrowserClient.search().
            asset_key: Asset key to use for COG URLs (e.g., "visual").
            render_settings: Optional dict with render mode and parameters.
        """
        self.clear()

        if not items:
            QMessageBox.warning(self, "STAC Explorer", "No items to load.")
            return

        self._prepare_gdal_for_loading()

        # Build list of (url, name, item_dict) to load
        layer_specs = []
        for item in items:
            asset = item["assets"].get(asset_key)
            if asset:
                layer_name = f"{item['date_str']}_{asset_key}"
                layer_specs.append((asset["href"], layer_name, item))

        if not layer_specs:
            QMessageBox.warning(
                self,
                "STAC Explorer",
                f"No items have asset '{asset_key}'.",
            )
            return

        self._pending_asset_key = asset_key
        self._render_settings = render_settings

        # Show animated (indeterminate) progress
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # indeterminate
        self.status_label.setVisible(True)
        self.status_label.setText("Preparing layers...")
        self.date_label.setText("Loading...")

        # Pre-fetch in background thread
        worker = TimeSliderLoadWorker(layer_specs)
        worker.progress.connect(self._on_load_progress)
        worker.finished.connect(self._on_load_finished)
        self._start_worker(worker)

    def _on_load_progress(self, current, total, name):
        """Handle layer loading progress.

        Args:
            current: Current item number (1-based).
            total: Total number of items.
            name: Name of the layer being loaded.
        """
        self.status_label.setText(f"Loading {name} ({current}/{total})...")

    def _on_load_finished(self, results):
        """Handle completed layer pre-fetch and start adding layers.

        Args:
            results: List of (cog_url, layer_name, item_dict, is_valid) tuples.
        """
        root = QgsProject.instance().layerTreeRoot()
        self._layer_group = root.insertGroup(0, "STAC Explorer Time Slider")

        self._time_steps = []
        self._pending_add = list(results)
        self._add_total = len(results)
        self.status_label.setText("Adding layers to map...")
        QTimer.singleShot(0, self._add_next_layer)

    def _add_next_layer(self):
        """Add the next pre-fetched layer to the time slider group."""
        if not self._pending_add:
            self._finish_layer_add()
            return

        cog_url, layer_name, item_dict, valid = self._pending_add.pop(0)
        idx = self._add_total - len(self._pending_add)
        self.status_label.setText(f"Adding layer {idx}/{self._add_total} to map...")

        # Ensure GDAL config is set before QgsRasterLayer creation
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")

        # Always attempt layer creation even if pre-check failed
        uri = f"/vsicurl/{cog_url}"
        layer = QgsRasterLayer(uri, layer_name, "gdal")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer, False)
            self._layer_group.addLayer(layer)
            if self._render_settings:
                self._apply_render_settings(layer, self._render_settings)
            self._time_steps.append(
                {
                    "datetime": item_dict["datetime"],
                    "date_str": item_dict["date_str"],
                    "layer_id": layer.id(),
                    "layer_name": layer_name,
                    "cog_url": cog_url,
                }
            )
        else:
            _logger.warning(
                "Failed to load time slider layer '%s' from %s: %s",
                layer_name,
                cog_url,
                layer.error().summary() if layer.error() else "unknown error",
            )

        if self._pending_add:
            QTimer.singleShot(0, self._add_next_layer)
        else:
            self._finish_layer_add()

    def _finish_layer_add(self):
        """Finalize time slider loading and enable controls."""
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)

        if not self._time_steps:
            self.date_label.setText("No data loaded")
            QMessageBox.warning(self, "STAC Explorer", "No valid layers were loaded.")
            return

        # Configure slider
        self.slider.setMaximum(len(self._time_steps) - 1)
        self.slider.setValue(0)
        self.slider.setEnabled(True)

        # Enable controls
        self.first_btn.setEnabled(True)
        self.prev_btn.setEnabled(True)
        self.next_btn.setEnabled(True)
        self.last_btn.setEnabled(True)
        self.play_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

        # Show first layer
        self._show_layer_at_index(0)

        self.iface.messageBar().pushMessage(
            "STAC Explorer",
            f"Loaded {len(self._time_steps)} time steps",
            level=0,
            duration=3,
        )

    def _apply_render_settings(self, layer, settings):
        """Apply render settings to a raster layer.

        Args:
            layer: QgsRasterLayer to style.
            settings: Dict with 'mode' and render parameters.
        """
        mode = settings.get("mode", "singleband")
        if mode == "rgb":
            renderer = QgsMultiBandColorRenderer(
                layer.dataProvider(),
                settings.get("red_band", 1),
                settings.get("green_band", 2),
                settings.get("blue_band", 3),
            )
            layer.setRenderer(renderer)
        else:
            ramp_name = settings.get("colormap", "RdYlGn")
            if ramp_name == "None":
                return

            min_val = settings.get("min_val", 0)
            max_val = settings.get("max_val", 250)

            style = QgsStyle.defaultStyle()
            color_ramp = style.colorRamp(ramp_name)
            if not color_ramp:
                return

            shader = QgsRasterShader()
            color_ramp_shader = QgsColorRampShader(min_val, max_val)
            color_ramp_shader.setColorRampType(QgsColorRampShader.Interpolated)
            color_ramp_shader.setSourceColorRamp(color_ramp)
            color_ramp_shader.classifyColorRamp(5)
            shader.setRasterShaderFunction(color_ramp_shader)

            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            renderer.setClassificationMin(min_val)
            renderer.setClassificationMax(max_val)
            layer.setRenderer(renderer)

        layer.triggerRepaint()

    def get_time_steps(self):
        """Get the list of loaded time steps.

        Returns:
            List of time step dicts with 'datetime', 'date_str', 'layer_id',
            'layer_name', and 'cog_url' keys.
        """
        return list(self._time_steps)

    def clear(self):
        """Remove all loaded layers and reset the slider."""
        self._stop_play()

        if self._layer_group:
            root = QgsProject.instance().layerTreeRoot()
            root.removeChildNode(self._layer_group)
            self._layer_group = None

        self._time_steps = []
        self.slider.setMaximum(0)
        self.slider.setValue(0)
        self.slider.setEnabled(False)
        self.date_label.setText("No data loaded")
        self.step_label.setText("")

        self.first_btn.setEnabled(False)
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.last_btn.setEnabled(False)
        self.play_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

    def _on_slider_changed(self, value):
        """Handle slider value change.

        Args:
            value: New slider value (index into time steps).
        """
        self._show_layer_at_index(value)

    def _show_layer_at_index(self, index):
        """Show only the layer at the given index, hiding all others.

        Args:
            index: Index into the time steps list.
        """
        if not self._time_steps or not self._layer_group:
            return

        if index < 0 or index >= len(self._time_steps):
            return

        step = self._time_steps[index]
        self.date_label.setText(step["date_str"])
        self.step_label.setText(f"Step {index + 1} / {len(self._time_steps)}")

        # Toggle layer visibility in the group
        for i, child in enumerate(self._layer_group.children()):
            child.setItemVisibilityChecked(i == index)

    def _go_first(self):
        """Jump to the first time step."""
        self.slider.setValue(0)

    def _go_previous(self):
        """Go to the previous time step."""
        current = self.slider.value()
        if current > 0:
            self.slider.setValue(current - 1)

    def _go_next(self):
        """Go to the next time step."""
        current = self.slider.value()
        if current < self.slider.maximum():
            self.slider.setValue(current + 1)

    def _go_last(self):
        """Jump to the last time step."""
        self.slider.setValue(self.slider.maximum())

    def _toggle_play(self):
        """Toggle auto-play on/off."""
        if self._auto_play_timer.isActive():
            self._stop_play()
        else:
            interval = self.speed_spin.value() * 1000
            self._auto_play_timer.start(interval)
            self.play_btn.setText("Pause")
            self.stop_btn.setEnabled(True)

    def _stop_play(self):
        """Stop auto-play."""
        self._auto_play_timer.stop()
        self.play_btn.setText("Play")
        self.stop_btn.setEnabled(False)

    def _auto_play_step(self):
        """Advance to the next step during auto-play."""
        current = self.slider.value()
        if current < self.slider.maximum():
            self.slider.setValue(current + 1)
        else:
            self.slider.setValue(0)  # Loop back to start
