"""
Time Series Dock Widget for STAC Explorer Plugin

Provides interactive time series plotting by clicking map locations.
Extracts pixel values from all time-step layers and displays a
matplotlib chart with export capabilities.
"""

import csv
from datetime import datetime

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QMessageBox,
)
from qgis.core import (
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsRaster,
)
from qgis.gui import QgsMapToolEmitPoint

try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure

    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


class TimeSeriesDockWidget(QDockWidget):
    """Time series dock widget for interactive pixel value plotting."""

    def __init__(self, iface, get_time_steps, parent=None):
        """Initialize the time series dock.

        Args:
            iface: QGIS interface instance.
            get_time_steps: Callable that returns time steps from the time slider.
            parent: Parent widget.
        """
        super().__init__("STAC Explorer Time Series", parent)
        self.iface = iface
        self._get_time_steps = get_time_steps
        self._map_tool = None
        self._previous_tool = None
        self._last_data = None
        self._last_point = None

        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self._setup_ui()

    def _setup_ui(self):
        """Set up the dock widget UI."""
        container = QWidget()
        layout = QVBoxLayout(container)

        # Point tool controls
        tool_group = QGroupBox("Point Selection")
        tool_layout = QVBoxLayout(tool_group)

        self.activate_btn = QPushButton("Activate Point Tool")
        self.activate_btn.setCheckable(True)
        self.activate_btn.clicked.connect(self._toggle_point_tool)
        tool_layout.addWidget(self.activate_btn)

        coord_layout = QHBoxLayout()
        coord_layout.addWidget(QLabel("Lat:"))
        self.lat_label = QLabel("--")
        coord_layout.addWidget(self.lat_label)
        coord_layout.addWidget(QLabel("Lon:"))
        self.lon_label = QLabel("--")
        coord_layout.addWidget(self.lon_label)
        tool_layout.addLayout(coord_layout)

        layout.addWidget(tool_group)

        # Band selection
        band_layout = QHBoxLayout()
        band_layout.addWidget(QLabel("Band:"))
        self.band_combo = QComboBox()
        self.band_combo.addItem("Band 1", 1)
        band_layout.addWidget(self.band_combo)
        layout.addLayout(band_layout)

        # Matplotlib canvas
        if HAS_MATPLOTLIB:
            self.figure = Figure(figsize=(5, 3))
            self.canvas = FigureCanvas(self.figure)
            self.ax = self.figure.add_subplot(111)
            self.ax.set_xlabel("Date")
            self.ax.set_ylabel("Value")
            self.ax.set_title("Time Series")
            self.figure.tight_layout()
            layout.addWidget(self.canvas)
        else:
            no_mpl_label = QLabel(
                "matplotlib is required for plotting.\n"
                "Install with: pip install matplotlib"
            )
            no_mpl_label.setAlignment(Qt.AlignCenter)
            no_mpl_label.setStyleSheet("color: red;")
            layout.addWidget(no_mpl_label)

        # Export buttons
        export_layout = QHBoxLayout()

        self.export_csv_btn = QPushButton("Export CSV")
        self.export_csv_btn.clicked.connect(self._export_csv)
        self.export_csv_btn.setEnabled(False)
        export_layout.addWidget(self.export_csv_btn)

        self.save_plot_btn = QPushButton("Save Plot")
        self.save_plot_btn.clicked.connect(self._save_plot)
        self.save_plot_btn.setEnabled(False)
        export_layout.addWidget(self.save_plot_btn)

        layout.addLayout(export_layout)

        self.setWidget(container)

    def _toggle_point_tool(self, checked):
        """Toggle the map point selection tool.

        Args:
            checked: Whether the button is checked.
        """
        if checked:
            self._activate_point_tool()
        else:
            self._deactivate_point_tool()

    def _activate_point_tool(self):
        """Activate the map point selection tool."""
        canvas = self.iface.mapCanvas()
        self._previous_tool = canvas.mapTool()
        self._map_tool = QgsMapToolEmitPoint(canvas)
        self._map_tool.canvasClicked.connect(self._on_map_clicked)
        canvas.setMapTool(self._map_tool)
        self.activate_btn.setText("Deactivate Point Tool")

    def _deactivate_point_tool(self):
        """Deactivate the map point selection tool."""
        canvas = self.iface.mapCanvas()
        if self._previous_tool:
            canvas.setMapTool(self._previous_tool)
        self._map_tool = None
        self.activate_btn.setText("Activate Point Tool")
        self.activate_btn.setChecked(False)

    def _on_map_clicked(self, point, button):
        """Handle map click event.

        Extracts pixel values from all time-step layers at the clicked
        point and plots the time series.

        Args:
            point: QgsPointXY of the clicked location.
            button: Mouse button used.
        """
        time_steps = self._get_time_steps()
        if not time_steps:
            QMessageBox.warning(
                self,
                "STAC Explorer",
                "No time steps available. Load data using the Time Slider first.",
            )
            return

        # Transform click point to WGS84 for display
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if canvas_crs != wgs84:
            transform = QgsCoordinateTransform(canvas_crs, wgs84, QgsProject.instance())
            display_point = transform.transform(point)
        else:
            display_point = point

        self.lat_label.setText(f"{display_point.y():.4f}")
        self.lon_label.setText(f"{display_point.x():.4f}")

        # Show wait cursor while extracting pixel values
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            self._extract_and_plot(point, display_point, canvas_crs, time_steps)
        finally:
            QApplication.restoreOverrideCursor()

    def _extract_and_plot(self, point, display_point, canvas_crs, time_steps):
        """Extract pixel values from time-step layers and plot.

        Args:
            point: QgsPointXY in canvas CRS.
            display_point: QgsPointXY in WGS84 for display.
            canvas_crs: CRS of the map canvas.
            time_steps: List of time step dicts.
        """
        # Extract values from each time step layer
        band = self.band_combo.currentData() or 1
        data = []

        for step in time_steps:
            layer = QgsProject.instance().mapLayer(step["layer_id"])
            if not layer:
                continue

            # Transform point to layer CRS
            layer_crs = layer.crs()
            if canvas_crs != layer_crs:
                transform = QgsCoordinateTransform(
                    canvas_crs, layer_crs, QgsProject.instance()
                )
                layer_point = transform.transform(point)
            else:
                layer_point = point

            result = layer.dataProvider().identify(
                QgsPointXY(layer_point), QgsRaster.IdentifyFormatValue
            )

            if result.isValid():
                values = result.results()
                value = values.get(band)
                if value is not None:
                    data.append(
                        {
                            "date_str": step["date_str"],
                            "datetime": step["datetime"],
                            "value": float(value),
                        }
                    )

        if not data:
            self.iface.messageBar().pushMessage(
                "STAC Explorer",
                "No valid pixel values found at this location.",
                level=1,
                duration=3,
            )
            return

        self._last_data = data
        self._last_point = display_point
        self._plot_time_series(data, display_point)

    def _plot_time_series(self, data, point):
        """Plot the time series data.

        Args:
            data: List of dicts with 'date_str', 'datetime', and 'value' keys.
            point: QgsPointXY of the clicked location.
        """
        if not HAS_MATPLOTLIB:
            return

        self.ax.clear()

        dates = []
        values = []
        for d in data:
            try:
                dt = datetime.fromisoformat(d["datetime"])
                dates.append(dt)
            except (ValueError, TypeError):
                dates.append(d["date_str"])
            values.append(d["value"])

        self.ax.plot(dates, values, "o-", color="#1565C0", markersize=4, linewidth=1.5)
        self.ax.set_xlabel("Date")
        self.ax.set_ylabel("Value")
        self.ax.set_title(f"Time Series ({point.y():.3f}, {point.x():.3f})")

        # Rotate x labels
        self.figure.autofmt_xdate()
        self.figure.tight_layout()
        self.canvas.draw()

        self.export_csv_btn.setEnabled(True)
        self.save_plot_btn.setEnabled(True)

    def _export_csv(self):
        """Export time series data to CSV."""
        if not self._last_data:
            return

        filepath, _ = QFileDialog.getSaveFileName(
            self, "Save Time Series CSV", "", "CSV Files (*.csv)"
        )
        if not filepath:
            return

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "value"])
            for d in self._last_data:
                writer.writerow([d["date_str"], d["value"]])

        self.iface.messageBar().pushMessage(
            "STAC Explorer",
            f"CSV exported to {filepath}",
            level=0,
            duration=3,
        )

    def _save_plot(self):
        """Save the current plot to an image file."""
        if not HAS_MATPLOTLIB:
            return

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Save Plot",
            "",
            "PNG Files (*.png);;PDF Files (*.pdf);;SVG Files (*.svg)",
        )
        if not filepath:
            return

        self.figure.savefig(filepath, dpi=150, bbox_inches="tight")

        self.iface.messageBar().pushMessage(
            "STAC Explorer",
            f"Plot saved to {filepath}",
            level=0,
            duration=3,
        )

    def closeEvent(self, event):
        """Handle dock close event.

        Args:
            event: The close event.
        """
        self._deactivate_point_tool()
        super().closeEvent(event)
