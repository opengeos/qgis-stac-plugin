"""
Search Dock Widget for STAC Explorer Plugin

Provides a STAC search panel with multi-catalog selection, collection browsing,
bounding box, date range, cloud cover filters, result loading, and asset download.
"""

import json
import logging
import os
from urllib.request import urlretrieve

from osgeo import gdal
from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    Qt,
    QDate,
    QThread,
    QTimer,
    QSize,
    QSettings,
    QVariant,
    pyqtSignal,
)
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QDateEdit,
    QPushButton,
    QToolButton,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QProgressBar,
    QHeaderView,
    QAbstractItemView,
    QSizePolicy,
    QMessageBox,
    QGroupBox,
    QFileDialog,
)
from qgis.core import (
    Qgis,
    QgsRasterLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsField,
    QgsPointXY,
    QgsFillSymbol,
    QgsRasterShader,
    QgsColorRampShader,
    QgsSingleBandPseudoColorRenderer,
    QgsMultiBandColorRenderer,
    QgsStyle,
)

from ..stac_client import DEFAULT_CATALOGS

_logger = logging.getLogger(__name__)


class CollectionFetchWorker(QThread):
    """Worker thread for fetching STAC collections."""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, stac):
        """Initialize the worker.

        Args:
            stac: STACBrowserClient instance.
        """
        super().__init__()
        self.stac = stac

    def run(self):
        """Fetch collections from the STAC API."""
        try:
            collections = self.stac.get_collections()
            self.finished.emit(collections)
        except Exception as e:
            self.error.emit(str(e))


class AssetKeyFetchWorker(QThread):
    """Worker thread for fetching asset keys for a collection."""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, stac, collection_id):
        """Initialize the worker.

        Args:
            stac: STACBrowserClient instance.
            collection_id: Collection ID to inspect.
        """
        super().__init__()
        self.stac = stac
        self.collection_id = collection_id

    def run(self):
        """Fetch asset keys from the STAC API."""
        try:
            keys = self.stac.get_collection_asset_keys(self.collection_id)
            self.finished.emit(keys)
        except Exception as e:
            self.error.emit(str(e))


class SearchWorker(QThread):
    """Worker thread for executing STAC searches."""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, stac, collections, bbox, datetime_range, max_cloud_cover, limit):
        """Initialize the search worker.

        Args:
            stac: STACBrowserClient instance.
            collections: List of collection IDs.
            bbox: Bounding box [west, south, east, north].
            datetime_range: Tuple of (start, end) date strings.
            max_cloud_cover: Maximum cloud cover percentage.
            limit: Maximum number of results.
        """
        super().__init__()
        self.stac = stac
        self.collections = collections
        self.bbox = bbox
        self.datetime_range = datetime_range
        self.max_cloud_cover = max_cloud_cover
        self.limit = limit

    def run(self):
        """Execute the STAC search."""
        try:
            items = self.stac.search(
                collections=self.collections,
                bbox=self.bbox,
                datetime_range=self.datetime_range,
                max_cloud_cover=self.max_cloud_cover,
                limit=self.limit,
            )
            self.finished.emit(items)
        except Exception as e:
            self.error.emit(str(e))


class LayerLoadWorker(QThread):
    """Worker thread that pre-fetches remote raster metadata via GDAL.

    Opens each URL with gdal.Open in a background thread to populate
    the VSI cache, so subsequent QgsRasterLayer creation on the main
    thread is fast.
    """

    progress = pyqtSignal(int, int, str)  # current, total, layer_name
    finished = pyqtSignal(list)  # list of (cog_url, layer_name, is_valid)

    def __init__(self, layer_specs):
        """Initialize the worker.

        Args:
            layer_specs: List of (cog_url, layer_name) tuples.
        """
        super().__init__()
        self.layer_specs = layer_specs

    def run(self):
        """Pre-fetch remote raster metadata for each layer."""
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        results = []
        total = len(self.layer_specs)
        for i, (cog_url, layer_name) in enumerate(self.layer_specs):
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
            results.append((cog_url, layer_name, valid))
        self.finished.emit(results)


class DownloadWorker(QThread):
    """Worker thread for downloading STAC assets to local files."""

    progress = pyqtSignal(int, int, str)  # current, total, filename
    finished = pyqtSignal(int, int)  # downloaded_count, failed_count
    error = pyqtSignal(str)

    def __init__(self, download_specs, output_dir):
        """Initialize the download worker.

        Args:
            download_specs: List of (url, filename) tuples.
            output_dir: Directory to save downloaded files.
        """
        super().__init__()
        self.download_specs = download_specs
        self.output_dir = output_dir

    def run(self):
        """Download all assets."""
        downloaded = 0
        failed = 0
        total = len(self.download_specs)

        for i, (url, filename) in enumerate(self.download_specs):
            self.progress.emit(i + 1, total, filename)
            output_path = os.path.join(self.output_dir, filename)
            try:
                urlretrieve(url, output_path)
                downloaded += 1
            except Exception:
                failed += 1

        self.finished.emit(downloaded, failed)


class SearchDockWidget(QDockWidget):
    """STAC search dock widget with catalog, collection, bbox, date, and cloud filters."""

    def __init__(self, iface, get_stac, load_to_time_slider, parent=None):
        """Initialize the search dock.

        Args:
            iface: QGIS interface instance.
            get_stac: Callable that returns the shared STACBrowserClient instance.
            load_to_time_slider: Callback to load items into the time slider.
            parent: Parent widget.
        """
        super().__init__("STAC Explorer Search", parent)
        self.iface = iface
        self._get_stac = get_stac
        self._load_to_time_slider = load_to_time_slider

        self._search_results = []
        self._workers = []
        self._footprint_layer = None
        self._updating_selection = False

        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

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
        if hasattr(worker, "error"):
            worker.error.connect(_cleanup)
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
        self._remove_footprint_layer()
        super().closeEvent(event)

    def _setup_ui(self):
        """Set up the dock widget UI."""
        container = QWidget()
        layout = QVBoxLayout(container)

        # --- Catalog selection (NEW) ---
        catalog_group = QGroupBox("Catalog")
        catalog_layout = QVBoxLayout(catalog_group)

        self.catalog_combo = QComboBox()
        self.catalog_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._populate_catalog_combo()
        self.catalog_combo.currentIndexChanged.connect(self._on_catalog_changed)
        catalog_layout.addWidget(self.catalog_combo)

        custom_row = QHBoxLayout()
        self.custom_url_edit = QLineEdit()
        self.custom_url_edit.setPlaceholderText("Custom STAC API URL...")
        custom_row.addWidget(self.custom_url_edit)

        self.add_catalog_btn = QPushButton("Add")
        self.add_catalog_btn.clicked.connect(self._add_custom_catalog)
        custom_row.addWidget(self.add_catalog_btn)
        catalog_layout.addLayout(custom_row)

        layout.addWidget(catalog_group)

        # --- Collection selection ---
        collection_group = QGroupBox("Collection")
        collection_layout = QVBoxLayout(collection_group)

        coll_row = QHBoxLayout()
        self.collection_combo = QComboBox()
        self.collection_combo.setEditable(True)
        self.collection_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.collection_combo.currentTextChanged.connect(self._on_collection_changed)
        coll_row.addWidget(self.collection_combo, 1)

        self.refresh_collections_btn = QToolButton()
        self.refresh_collections_btn.setIcon(
            QIcon(":/images/themes/default/mActionRefresh.svg")
        )
        self.refresh_collections_btn.setToolTip("Refresh collections")
        self.refresh_collections_btn.setIconSize(QSize(20, 20))
        self.refresh_collections_btn.clicked.connect(self._fetch_collections)
        coll_row.addWidget(self.refresh_collections_btn)
        collection_layout.addLayout(coll_row)

        # Collection ID label
        self.collection_id_label = QLabel("")
        self.collection_id_label.setStyleSheet("color: gray; font-size: 10px;")
        self.collection_id_label.setWordWrap(True)
        collection_layout.addWidget(self.collection_id_label)

        # Asset key
        asset_row = QHBoxLayout()
        asset_row.addWidget(QLabel("Asset:"))
        self.asset_combo = QComboBox()
        self.asset_combo.setEditable(True)
        asset_row.addWidget(self.asset_combo)
        collection_layout.addLayout(asset_row)

        # Render mode
        render_row = QHBoxLayout()
        render_row.addWidget(QLabel("Render:"))
        self.render_mode_combo = QComboBox()
        self.render_mode_combo.addItem("Single Band (Colormap)", "singleband")
        self.render_mode_combo.addItem("RGB Composite", "rgb")
        self.render_mode_combo.currentIndexChanged.connect(self._on_render_mode_changed)
        render_row.addWidget(self.render_mode_combo)
        collection_layout.addLayout(render_row)

        # Colormap selection
        self.colormap_row = QHBoxLayout()
        self.colormap_row_label = QLabel("Colormap:")
        self.colormap_row.addWidget(self.colormap_row_label)
        self.colormap_combo = QComboBox()
        self._populate_colormaps()
        self.colormap_row.addWidget(self.colormap_combo)
        collection_layout.addLayout(self.colormap_row)

        # Min/Max for colormap
        self.min_max_row = QHBoxLayout()
        self.min_max_row.addWidget(QLabel("Min:"))
        self.colormap_min_spin = QDoubleSpinBox()
        self.colormap_min_spin.setRange(-10000, 10000)
        self.colormap_min_spin.setDecimals(2)
        self.colormap_min_spin.setValue(0)
        self.min_max_row.addWidget(self.colormap_min_spin)
        self.min_max_row.addWidget(QLabel("Max:"))
        self.colormap_max_spin = QDoubleSpinBox()
        self.colormap_max_spin.setRange(-10000, 10000)
        self.colormap_max_spin.setDecimals(2)
        self.colormap_max_spin.setValue(250)
        self.min_max_row.addWidget(self.colormap_max_spin)
        collection_layout.addLayout(self.min_max_row)

        # RGB band selection (hidden by default)
        self.rgb_row = QHBoxLayout()
        self.rgb_row_label = QLabel("Bands (R,G,B):")
        self.rgb_row.addWidget(self.rgb_row_label)
        self.red_band_spin = QSpinBox()
        self.red_band_spin.setRange(1, 20)
        self.red_band_spin.setValue(1)
        self.rgb_row.addWidget(self.red_band_spin)
        self.green_band_spin = QSpinBox()
        self.green_band_spin.setRange(1, 20)
        self.green_band_spin.setValue(2)
        self.rgb_row.addWidget(self.green_band_spin)
        self.blue_band_spin = QSpinBox()
        self.blue_band_spin.setRange(1, 20)
        self.blue_band_spin.setValue(3)
        self.rgb_row.addWidget(self.blue_band_spin)
        collection_layout.addLayout(self.rgb_row)

        # Initially show single band controls, hide RGB
        self._set_rgb_visible(False)

        layout.addWidget(collection_group)

        # --- Bounding box ---
        bbox_group = QGroupBox("Bounding Box (WGS84)")
        bbox_layout = QFormLayout(bbox_group)

        self.west_spin = QDoubleSpinBox()
        self.west_spin.setRange(-180.0, 180.0)
        self.west_spin.setDecimals(4)
        bbox_layout.addRow("West:", self.west_spin)

        self.south_spin = QDoubleSpinBox()
        self.south_spin.setRange(-90.0, 90.0)
        self.south_spin.setDecimals(4)
        bbox_layout.addRow("South:", self.south_spin)

        self.east_spin = QDoubleSpinBox()
        self.east_spin.setRange(-180.0, 180.0)
        self.east_spin.setDecimals(4)
        bbox_layout.addRow("East:", self.east_spin)

        self.north_spin = QDoubleSpinBox()
        self.north_spin.setRange(-90.0, 90.0)
        self.north_spin.setDecimals(4)
        bbox_layout.addRow("North:", self.north_spin)

        self.use_canvas_btn = QPushButton("Use Map Canvas Extent")
        self.use_canvas_btn.clicked.connect(self._use_canvas_extent)
        bbox_layout.addRow(self.use_canvas_btn)

        layout.addWidget(bbox_group)

        # --- Date range ---
        date_group = QGroupBox("Date Range")
        date_layout = QFormLayout(date_group)

        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addMonths(-3))
        date_layout.addRow("Start:", self.start_date)

        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        date_layout.addRow("End:", self.end_date)

        layout.addWidget(date_group)

        # --- Filters (defaults from QSettings) ---
        settings = QSettings()
        filter_group = QGroupBox("Filters")
        filter_layout = QFormLayout(filter_group)

        self.cloud_cover_spin = QSpinBox()
        self.cloud_cover_spin.setRange(0, 100)
        self.cloud_cover_spin.setValue(
            int(settings.value("STACExplorer/default_cloud_cover", 30))
        )
        self.cloud_cover_spin.setSuffix("%")
        filter_layout.addRow("Max cloud cover:", self.cloud_cover_spin)

        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(1, 500)
        self.max_results_spin.setValue(
            int(settings.value("STACExplorer/default_max_results", 50))
        )
        filter_layout.addRow("Max results:", self.max_results_spin)

        layout.addWidget(filter_group)

        # --- Search button ---
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._on_search)
        layout.addWidget(self.search_btn)

        # Progress bar and status label
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        # --- Results table ---
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(3)
        self.results_table.setHorizontalHeaderLabels(["Date", "ID", "Cloud %"])
        self.results_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.results_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.results_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSortingEnabled(True)
        self.results_table.horizontalHeader().setSectionsClickable(True)
        self.results_table.itemSelectionChanged.connect(
            self._on_table_selection_changed
        )
        layout.addWidget(self.results_table)

        # --- Action buttons ---
        action_layout = QHBoxLayout()

        self.load_selected_btn = QPushButton("Load Selected")
        self.load_selected_btn.clicked.connect(self._load_selected)
        self.load_selected_btn.setEnabled(False)
        action_layout.addWidget(self.load_selected_btn)

        self.load_all_btn = QPushButton("Load to Time Slider")
        self.load_all_btn.clicked.connect(self._load_selected_to_time_slider)
        self.load_all_btn.setEnabled(False)
        action_layout.addWidget(self.load_all_btn)

        self.download_btn = QPushButton("Download Selected")
        self.download_btn.clicked.connect(self._download_selected)
        self.download_btn.setEnabled(False)
        action_layout.addWidget(self.download_btn)

        layout.addLayout(action_layout)

        self.setWidget(container)

    # --- Catalog management ---

    def _populate_catalog_combo(self):
        """Populate the catalog combo with default and custom catalogs."""
        self.catalog_combo.clear()
        for name, url in DEFAULT_CATALOGS.items():
            self.catalog_combo.addItem(name, url)

        # Load custom catalogs from QSettings
        settings = QSettings()
        custom_json = settings.value("STACExplorer/custom_catalogs", "[]")
        try:
            custom_catalogs = json.loads(custom_json)
        except (json.JSONDecodeError, TypeError):
            custom_catalogs = []

        for entry in custom_catalogs:
            if isinstance(entry, dict):
                self.catalog_combo.addItem(
                    entry.get("name", entry["url"]), entry["url"]
                )
            elif isinstance(entry, str):
                self.catalog_combo.addItem(entry, entry)

    def _on_catalog_changed(self, index):
        """Handle catalog selection change.

        Args:
            index: Selected combo index.
        """
        url = self.catalog_combo.itemData(index)
        if url:
            stac = self._get_stac()
            stac.set_catalog(url)
            self._fetch_collections()

    def _add_custom_catalog(self):
        """Add a custom catalog URL from the text input."""
        url = self.custom_url_edit.text().strip()
        if not url:
            return

        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self, "STAC Explorer", "Please enter a valid URL starting with http(s)."
            )
            return

        # Check if already in the combo
        for i in range(self.catalog_combo.count()):
            if self.catalog_combo.itemData(i) == url:
                self.catalog_combo.setCurrentIndex(i)
                self.custom_url_edit.clear()
                return

        # Add to combo and persist
        self.catalog_combo.addItem(url, url)
        self.catalog_combo.setCurrentIndex(self.catalog_combo.count() - 1)
        self.custom_url_edit.clear()

        self._save_custom_catalogs()

    def _save_custom_catalogs(self):
        """Save custom catalogs to QSettings."""
        custom_catalogs = []
        default_urls = set(DEFAULT_CATALOGS.values())
        for i in range(self.catalog_combo.count()):
            url = self.catalog_combo.itemData(i)
            if url and url not in default_urls:
                name = self.catalog_combo.itemText(i)
                custom_catalogs.append({"name": name, "url": url})

        settings = QSettings()
        settings.setValue("STACExplorer/custom_catalogs", json.dumps(custom_catalogs))

    # --- Collection management ---

    def showEvent(self, event):
        """Handle dock show event to fetch collections.

        Args:
            event: The show event.
        """
        super().showEvent(event)
        if self.collection_combo.count() == 0:
            self._fetch_collections()

    def _fetch_collections(self):
        """Fetch available collections from the STAC API."""
        self.refresh_collections_btn.setEnabled(False)
        self.collection_combo.clear()
        self.collection_combo.addItem("Loading collections...")

        try:
            stac = self._get_stac()
            worker = CollectionFetchWorker(stac)
            worker.finished.connect(self._on_collections_fetched)
            worker.error.connect(self._on_collections_error)
            self._start_worker(worker)
        except Exception as e:
            self.collection_combo.clear()
            self.collection_combo.addItem(f"Error: {e}")
            self.refresh_collections_btn.setEnabled(True)

    def _on_collections_fetched(self, collections):
        """Handle fetched collections.

        Args:
            collections: List of collection dicts.
        """
        self.collection_combo.clear()
        collections.sort(key=lambda c: c["title"].lower())
        for coll in collections:
            self.collection_combo.addItem(coll["title"], coll["id"])
        self.refresh_collections_btn.setEnabled(True)

        # Select default collection from settings
        default_id = QSettings().value("STACExplorer/default_collection", "")
        if default_id:
            for i in range(self.collection_combo.count()):
                if self.collection_combo.itemData(i) == default_id:
                    self.collection_combo.setCurrentIndex(i)
                    break

    def _on_collections_error(self, error_msg):
        """Handle collection fetch error.

        Args:
            error_msg: Error message string.
        """
        self.collection_combo.clear()
        self.collection_combo.addItem(f"Error: {error_msg}")
        self.refresh_collections_btn.setEnabled(True)

    def _on_collection_changed(self, text):
        """Handle collection selection change.

        Args:
            text: Selected collection text.
        """
        # Clear previous results and footprints
        self.results_table.setRowCount(0)
        self._search_results = []
        self.load_selected_btn.setEnabled(False)
        self.load_all_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self._remove_footprint_layer()

        idx = self.collection_combo.currentIndex()
        collection_id = self.collection_combo.itemData(idx)
        if collection_id:
            self.collection_id_label.setText(collection_id)
            self._fetch_asset_keys(collection_id)
        else:
            self.collection_id_label.setText("")

    def _fetch_asset_keys(self, collection_id):
        """Fetch asset keys for a collection.

        Args:
            collection_id: Collection ID to inspect.
        """
        self.asset_combo.clear()
        self.asset_combo.addItem("Loading...")

        try:
            stac = self._get_stac()
            worker = AssetKeyFetchWorker(stac, collection_id)
            worker.finished.connect(self._on_asset_keys_fetched)
            worker.error.connect(self._on_asset_keys_error)
            self._start_worker(worker)
        except Exception as e:
            self.asset_combo.clear()
            self.asset_combo.addItem(f"Error: {e}")

    def _on_asset_keys_fetched(self, keys):
        """Handle fetched asset keys.

        Args:
            keys: List of asset key strings.
        """
        self.asset_combo.clear()
        for key in keys:
            self.asset_combo.addItem(key)

        # Default to "visual" if available, else first key
        visual_idx = self.asset_combo.findText("visual")
        if visual_idx >= 0:
            self.asset_combo.setCurrentIndex(visual_idx)

    def _on_asset_keys_error(self, error_msg):
        """Handle asset key fetch error.

        Args:
            error_msg: Error message string.
        """
        self.asset_combo.clear()
        self.asset_combo.addItem(f"Error: {error_msg}")

    # --- Render controls ---

    def _populate_colormaps(self):
        """Populate the colormap combo with available color ramps."""
        self.colormap_combo.addItem("None", "none")

        style = QgsStyle.defaultStyle()
        ramp_names = [
            "RdYlGn",
            "Spectral",
            "Greens",
            "RdYlBu",
            "Viridis",
            "Turbo",
            "Plasma",
            "Inferno",
            "Magma",
            "Blues",
            "Reds",
            "YlOrRd",
        ]
        for name in ramp_names:
            if style.colorRampNames().count(name) > 0:
                self.colormap_combo.addItem(name)
        # Fallback if none found
        if self.colormap_combo.count() == 1:
            for name in style.colorRampNames()[:12]:
                self.colormap_combo.addItem(name)

    def _on_render_mode_changed(self, index):
        """Handle render mode selection change.

        Args:
            index: Selected combo index.
        """
        mode = self.render_mode_combo.currentData()
        is_rgb = mode == "rgb"
        self._set_rgb_visible(is_rgb)
        self._set_colormap_visible(not is_rgb)

    def _set_rgb_visible(self, visible):
        """Show or hide RGB band controls.

        Args:
            visible: Whether to show RGB controls.
        """
        self.rgb_row_label.setVisible(visible)
        self.red_band_spin.setVisible(visible)
        self.green_band_spin.setVisible(visible)
        self.blue_band_spin.setVisible(visible)

    def _set_colormap_visible(self, visible):
        """Show or hide colormap controls.

        Args:
            visible: Whether to show colormap controls.
        """
        self.colormap_row_label.setVisible(visible)
        self.colormap_combo.setVisible(visible)
        self.colormap_min_spin.setVisible(visible)
        self.colormap_max_spin.setVisible(visible)

    def _apply_renderer(self, layer):
        """Apply the selected render mode to a raster layer.

        Args:
            layer: QgsRasterLayer to style.
        """
        mode = self.render_mode_combo.currentData()
        if mode == "rgb":
            self._apply_rgb_renderer(layer)
        else:
            self._apply_colormap_renderer(layer)

    def _apply_colormap_renderer(self, layer):
        """Apply single band pseudocolor renderer with the selected colormap.

        Args:
            layer: QgsRasterLayer to style.
        """
        if self.colormap_combo.currentData() == "none":
            return

        ramp_name = self.colormap_combo.currentText()
        style = QgsStyle.defaultStyle()
        color_ramp = style.colorRamp(ramp_name)
        if not color_ramp:
            return

        min_val = self.colormap_min_spin.value()
        max_val = self.colormap_max_spin.value()

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

    def _apply_rgb_renderer(self, layer):
        """Apply multi-band RGB renderer.

        Args:
            layer: QgsRasterLayer to style.
        """
        red = self.red_band_spin.value()
        green = self.green_band_spin.value()
        blue = self.blue_band_spin.value()

        renderer = QgsMultiBandColorRenderer(layer.dataProvider(), red, green, blue)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def _get_render_settings(self):
        """Get current render settings as a dict for passing to other docks.

        Returns:
            Dict with render mode and parameters.
        """
        mode = self.render_mode_combo.currentData()
        settings = {"mode": mode}
        if mode == "rgb":
            settings["red_band"] = self.red_band_spin.value()
            settings["green_band"] = self.green_band_spin.value()
            settings["blue_band"] = self.blue_band_spin.value()
        else:
            settings["colormap"] = self.colormap_combo.currentText()
            settings["min_val"] = self.colormap_min_spin.value()
            settings["max_val"] = self.colormap_max_spin.value()
        return settings

    # --- Bounding box ---

    def _use_canvas_extent(self):
        """Set bounding box from the current map canvas extent."""
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()

        # Transform to EPSG:4326
        canvas_crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        if canvas_crs != wgs84:
            transform = QgsCoordinateTransform(canvas_crs, wgs84, QgsProject.instance())
            extent = transform.transformBoundingBox(extent)

        self.west_spin.setValue(extent.xMinimum())
        self.south_spin.setValue(extent.yMinimum())
        self.east_spin.setValue(extent.xMaximum())
        self.north_spin.setValue(extent.yMaximum())

    # --- Search ---

    def _on_search(self):
        """Execute a STAC search with current parameters."""
        idx = self.collection_combo.currentIndex()
        collection_id = self.collection_combo.itemData(idx)
        if not collection_id:
            collection_id = self.collection_combo.currentText().strip()
        if not collection_id:
            QMessageBox.warning(self, "STAC Explorer", "Please select a collection.")
            return

        bbox = [
            self.west_spin.value(),
            self.south_spin.value(),
            self.east_spin.value(),
            self.north_spin.value(),
        ]

        # Check for valid bbox
        if bbox[0] == 0 and bbox[1] == 0 and bbox[2] == 0 and bbox[3] == 0:
            reply = QMessageBox.question(
                self,
                "STAC Explorer",
                "Bounding box is all zeros. Search without spatial filter?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return
            bbox = None

        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")

        self._remove_footprint_layer()

        self.search_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.results_table.setRowCount(0)
        self.load_selected_btn.setEnabled(False)
        self.load_all_btn.setEnabled(False)
        self.download_btn.setEnabled(False)

        stac = self._get_stac()
        worker = SearchWorker(
            stac,
            [collection_id],
            bbox,
            (start, end),
            self.cloud_cover_spin.value(),
            self.max_results_spin.value(),
        )
        worker.finished.connect(self._on_search_finished)
        worker.error.connect(self._on_search_error)
        self._start_worker(worker)

    def _on_search_finished(self, items):
        """Handle search results.

        Args:
            items: List of item dicts from STACBrowserClient.search().
        """
        self.search_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._search_results = items

        # Disable sorting while populating to avoid index confusion
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(len(items))
        for row, item in enumerate(items):
            date_item = QTableWidgetItem(item["date_str"])
            date_item.setData(Qt.UserRole, row)
            self.results_table.setItem(row, 0, date_item)
            self.results_table.setItem(row, 1, QTableWidgetItem(item["id"]))
            cloud = item.get("cloud_cover")
            cloud_item = QTableWidgetItem()
            cloud_item.setData(Qt.DisplayRole, cloud if cloud is not None else "N/A")
            self.results_table.setItem(row, 2, cloud_item)
        self.results_table.setSortingEnabled(True)

        if items:
            self.load_selected_btn.setEnabled(True)
            self.load_all_btn.setEnabled(True)
            self.download_btn.setEnabled(True)
            self._create_footprint_layer(items)

        self.iface.messageBar().pushMessage(
            "STAC Explorer",
            f"Found {len(items)} items",
            level=0,
            duration=3,
        )

    def _on_search_error(self, error_msg):
        """Handle search error.

        Args:
            error_msg: Error message string.
        """
        self.search_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        QMessageBox.critical(self, "STAC Search Error", error_msg)

    # --- GDAL configuration ---

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

    # --- Load layers ---

    def _get_selected_data_indices(self):
        """Get original data indices from the selected table rows.

        Returns:
            Sorted list of data indices, or None if none selected.
        """
        selected_visual_rows = set()
        for item in self.results_table.selectedItems():
            selected_visual_rows.add(item.row())

        if not selected_visual_rows:
            QMessageBox.warning(
                self, "STAC Explorer", "Please select at least one result."
            )
            return None

        data_indices = []
        for visual_row in sorted(selected_visual_rows):
            date_item = self.results_table.item(visual_row, 0)
            if date_item is not None:
                data_indices.append(date_item.data(Qt.UserRole))

        return data_indices if data_indices else None

    def _get_valid_asset_key(self):
        """Get the currently selected asset key if valid.

        Returns:
            Asset key string, or None if not ready.
        """
        asset_key = self.asset_combo.currentText()
        if not asset_key or asset_key in ("Loading...", ""):
            QMessageBox.warning(
                self,
                "STAC Explorer",
                "Please wait for asset keys to load, then select one.",
            )
            return None
        return asset_key

    def _load_selected(self):
        """Load selected items as raster layers in the background."""
        data_indices = self._get_selected_data_indices()
        if data_indices is None:
            return

        asset_key = self._get_valid_asset_key()
        if asset_key is None:
            return

        self._prepare_gdal_for_loading()

        # Build list of (url, name) to load
        layer_specs = []
        for idx in data_indices:
            item = self._search_results[idx]
            asset = item["assets"].get(asset_key)
            if asset:
                layer_name = f"{item['date_str']}_{asset_key}"
                layer_specs.append((asset["href"], layer_name))

        if not layer_specs:
            QMessageBox.warning(
                self,
                "STAC Explorer",
                f"Asset '{asset_key}' not found in any selected items.",
            )
            return

        # Disable controls and show animated (indeterminate) progress
        self.load_selected_btn.setEnabled(False)
        self.load_all_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # indeterminate
        self.status_label.setVisible(True)
        self.status_label.setText("Preparing layers...")

        # Pre-fetch metadata in background thread
        worker = LayerLoadWorker(layer_specs)
        worker.progress.connect(self._on_layer_load_progress)
        worker.finished.connect(self._on_layers_loaded)
        self._start_worker(worker)

    def _on_layer_load_progress(self, current, total, name):
        """Handle layer loading progress.

        Args:
            current: Current item number (1-based).
            total: Total number of items.
            name: Name of the layer being loaded.
        """
        self.status_label.setText(f"Loading {name} ({current}/{total})...")

    def _on_layers_loaded(self, results):
        """Handle completed layer pre-fetch and add layers to project.

        Args:
            results: List of (cog_url, layer_name, is_valid) tuples.
        """
        self._pending_add = list(results)
        self._add_loaded = 0
        self._add_failed = 0
        self._add_total = len(results)
        self.status_label.setText("Adding layers to map...")
        QTimer.singleShot(0, self._add_next_layer)

    def _add_next_layer(self):
        """Add the next pre-fetched layer to the project."""
        if not self._pending_add:
            self._finish_layer_add()
            return

        cog_url, layer_name, valid = self._pending_add.pop(0)
        idx = self._add_total - len(self._pending_add)
        self.status_label.setText(f"Adding layer {idx}/{self._add_total} to map...")

        # Ensure GDAL config is set before QgsRasterLayer creation
        gdal.SetConfigOption("AWS_NO_SIGN_REQUEST", "YES")

        # Always attempt layer creation even if pre-check failed,
        # as GDAL config may differ between the worker thread and
        # the QGIS raster provider on the main thread.
        uri = f"/vsicurl/{cog_url}"
        layer = QgsRasterLayer(uri, layer_name, "gdal")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self._apply_renderer(layer)
            self._add_loaded += 1
        else:
            _logger.warning(
                "Failed to load layer '%s' from %s: %s",
                layer_name,
                cog_url,
                layer.error().summary() if layer.error() else "unknown error",
            )
            self._add_failed += 1

        if self._pending_add:
            QTimer.singleShot(0, self._add_next_layer)
        else:
            self._finish_layer_add()

    def _finish_layer_add(self):
        """Finalize layer loading and re-enable controls."""
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)
        self.load_selected_btn.setEnabled(True)
        self.load_all_btn.setEnabled(True)
        self.download_btn.setEnabled(True)
        self.search_btn.setEnabled(True)

        if self._add_loaded:
            self.iface.messageBar().pushMessage(
                "STAC Explorer",
                f"Loaded {self._add_loaded} layer(s)",
                level=0,
                duration=3,
            )
        if self._add_failed:
            self.iface.messageBar().pushMessage(
                "STAC Explorer",
                f"Failed to load {self._add_failed} layer(s). "
                "Check QGIS log for details (View > Log Messages).",
                level=1,
                duration=5,
            )

    def _load_selected_to_time_slider(self):
        """Load selected search results to the time slider."""
        data_indices = self._get_selected_data_indices()
        if data_indices is None:
            return

        asset_key = self._get_valid_asset_key()
        if asset_key is None:
            return

        self._prepare_gdal_for_loading()

        selected_items = [self._search_results[idx] for idx in data_indices]
        render_settings = self._get_render_settings()
        self._load_to_time_slider(selected_items, asset_key, render_settings)

    # --- Download ---

    def _download_selected(self):
        """Download selected items' assets to a local directory."""
        data_indices = self._get_selected_data_indices()
        if data_indices is None:
            return

        asset_key = self._get_valid_asset_key()
        if asset_key is None:
            return

        # Ask for download directory
        settings = QSettings()
        default_dir = settings.value("STACExplorer/default_download_dir", "")
        output_dir = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", default_dir
        )
        if not output_dir:
            return

        settings.setValue("STACExplorer/default_download_dir", output_dir)

        # Build download specs
        download_specs = []
        for idx in data_indices:
            item = self._search_results[idx]
            asset = item["assets"].get(asset_key)
            if asset:
                # Use date_str + item ID for unique filenames
                ext = ".tif"
                href = asset["href"]
                if "." in href.split("/")[-1]:
                    ext = "." + href.split("/")[-1].rsplit(".", 1)[-1]
                filename = f"{item['date_str']}_{item['id']}_{asset_key}{ext}"
                # Sanitize filename
                filename = filename.replace("/", "_").replace("\\", "_")
                download_specs.append((href, filename))

        if not download_specs:
            QMessageBox.warning(
                self,
                "STAC Explorer",
                f"Asset '{asset_key}' not found in any selected items.",
            )
            return

        # Disable controls and show progress
        self.download_btn.setEnabled(False)
        self.load_selected_btn.setEnabled(False)
        self.load_all_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(download_specs))
        self.progress_bar.setValue(0)
        self.status_label.setVisible(True)
        self.status_label.setText("Downloading...")

        worker = DownloadWorker(download_specs, output_dir)
        worker.progress.connect(self._on_download_progress)
        worker.finished.connect(self._on_download_finished)
        self._start_worker(worker)

    def _on_download_progress(self, current, total, filename):
        """Handle download progress.

        Args:
            current: Current item number (1-based).
            total: Total number of items.
            filename: Name of the file being downloaded.
        """
        self.progress_bar.setValue(current)
        self.status_label.setText(f"Downloading {filename} ({current}/{total})...")

    def _on_download_finished(self, downloaded, failed):
        """Handle download completion.

        Args:
            downloaded: Number of successfully downloaded files.
            failed: Number of failed downloads.
        """
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)
        self.download_btn.setEnabled(True)
        self.load_selected_btn.setEnabled(True)
        self.load_all_btn.setEnabled(True)
        self.search_btn.setEnabled(True)

        if downloaded:
            self.iface.messageBar().pushMessage(
                "STAC Explorer",
                f"Downloaded {downloaded} file(s)",
                level=0,
                duration=3,
            )
        if failed:
            self.iface.messageBar().pushMessage(
                "STAC Explorer",
                f"Failed to download {failed} file(s)",
                level=1,
                duration=5,
            )

    # --- Footprint layer ---

    def _create_footprint_layer(self, items):
        """Create a vector layer with footprint polygons for search results.

        Args:
            items: List of item dicts with 'geometry' keys.
        """
        self._remove_footprint_layer()

        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Search Footprints", "memory")
        dp = layer.dataProvider()
        dp.addAttributes(
            [
                QgsField("data_index", QVariant.Int),
                QgsField("date", QVariant.String),
                QgsField("item_id", QVariant.String),
                QgsField("cloud_cover", QVariant.Double),
            ]
        )
        layer.updateFields()

        features = []
        for i, item in enumerate(items):
            geom_dict = item.get("geometry")
            if not geom_dict:
                continue

            geom = QgsGeometry.fromPolygonXY(
                [
                    [QgsPointXY(c[0], c[1]) for c in ring]
                    for ring in geom_dict.get("coordinates", [])
                ]
            )

            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttribute("data_index", i)
            feat.setAttribute("date", item["date_str"])
            feat.setAttribute("item_id", item["id"])
            feat.setAttribute(
                "cloud_cover",
                item.get("cloud_cover") if item.get("cloud_cover") is not None else -1,
            )
            features.append(feat)

        dp.addFeatures(features)
        layer.updateExtents()

        # Style: semi-transparent blue outline
        symbol = QgsFillSymbol.createSimple(
            {
                "color": "65,105,225,40",
                "outline_color": "65,105,225,200",
                "outline_width": "0.5",
            }
        )
        layer.renderer().setSymbol(symbol)

        # Custom selection style: yellow fill + yellow outline
        try:
            sel_symbol = QgsFillSymbol.createSimple(
                {
                    "color": "255,255,0,100",
                    "outline_color": "255,255,0,255",
                    "outline_width": "1.0",
                }
            )
            props = layer.selectionProperties()
            props.setSelectionRenderingMode(Qgis.SelectionRenderingMode.CustomSymbol)
            props.setSelectionSymbol(sel_symbol)
            layer.setSelectionProperties(props)
        except AttributeError:
            # Fallback for QGIS < 3.34 without custom selection symbols
            pass

        layer.triggerRepaint()

        QgsProject.instance().addMapLayer(layer, True)
        self._footprint_layer = layer

        # Zoom to footprint layer extent (transform to canvas CRS)
        canvas = self.iface.mapCanvas()
        extent = layer.extent()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if layer.crs() != canvas_crs:
            xform = QgsCoordinateTransform(
                layer.crs(), canvas_crs, QgsProject.instance()
            )
            extent = xform.transformBoundingBox(extent)
        canvas.setExtent(extent)
        canvas.refresh()

        # Connect map feature selection to table
        layer.selectionChanged.connect(self._on_footprint_selection_changed)

    def _remove_footprint_layer(self):
        """Remove the footprint vector layer from the project."""
        if self._footprint_layer and not sip.isdeleted(self._footprint_layer):
            try:
                self._footprint_layer.selectionChanged.disconnect(
                    self._on_footprint_selection_changed
                )
            except Exception:
                pass
            QgsProject.instance().removeMapLayer(self._footprint_layer.id())
            self._footprint_layer = None

    def _is_footprint_layer_valid(self):
        """Check if the footprint layer still exists and is not deleted.

        Returns:
            True if the footprint layer is valid.
        """
        if not self._footprint_layer or sip.isdeleted(self._footprint_layer):
            self._footprint_layer = None
            return False
        return True

    def _on_table_selection_changed(self):
        """Sync table row selection to footprint layer feature selection."""
        if self._updating_selection or not self._is_footprint_layer_valid():
            return

        self._updating_selection = True
        try:
            # Get selected data indices from table
            selected_data_indices = set()
            for item in self.results_table.selectedItems():
                date_item = self.results_table.item(item.row(), 0)
                if date_item is not None:
                    idx = date_item.data(Qt.UserRole)
                    if idx is not None:
                        selected_data_indices.add(idx)

            # Find matching feature IDs
            fids = []
            for feat in self._footprint_layer.getFeatures():
                if feat["data_index"] in selected_data_indices:
                    fids.append(feat.id())

            self._footprint_layer.selectByIds(fids)

            # Zoom to selected footprints (transform to canvas CRS)
            if fids:
                bbox = self._footprint_layer.boundingBoxOfSelected()
                if not bbox.isEmpty():
                    bbox.scale(1.1)
                    canvas = self.iface.mapCanvas()
                    canvas_crs = canvas.mapSettings().destinationCrs()
                    if self._footprint_layer.crs() != canvas_crs:
                        xform = QgsCoordinateTransform(
                            self._footprint_layer.crs(),
                            canvas_crs,
                            QgsProject.instance(),
                        )
                        bbox = xform.transformBoundingBox(bbox)
                    canvas.setExtent(bbox)
                    canvas.refresh()
        finally:
            self._updating_selection = False

    def _on_footprint_selection_changed(self, selected, deselected, clear_and_select):
        """Sync footprint feature selection to table row selection.

        Args:
            selected: List of selected feature IDs.
            deselected: List of deselected feature IDs.
            clear_and_select: Whether this is a clear-and-select operation.
        """
        if self._updating_selection or not self._is_footprint_layer_valid():
            return

        self._updating_selection = True
        try:
            # Get data indices from selected features
            selected_data_indices = set()
            for fid in self._footprint_layer.selectedFeatureIds():
                feat = self._footprint_layer.getFeature(fid)
                selected_data_indices.add(feat["data_index"])

            # Select matching table rows
            self.results_table.clearSelection()
            for row in range(self.results_table.rowCount()):
                date_item = self.results_table.item(row, 0)
                if date_item is not None:
                    idx = date_item.data(Qt.UserRole)
                    if idx in selected_data_indices:
                        self.results_table.selectRow(row)
        finally:
            self._updating_selection = False
