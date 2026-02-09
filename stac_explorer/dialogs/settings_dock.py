"""
Settings Dock Widget for STAC Explorer Plugin

Provides catalog management (add/remove custom catalogs, set default)
and general settings (default collection, cloud cover, max results,
download directory, debug mode).
"""

import json

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QTabWidget,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QSpinBox,
    QGroupBox,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
)

from ..stac_client import DEFAULT_CATALOGS


class SettingsDockWidget(QDockWidget):
    """Settings dock widget with catalog management and configuration tabs."""

    def __init__(self, iface, get_stac, parent=None):
        """Initialize the settings dock.

        Args:
            iface: QGIS interface instance.
            get_stac: Callable that returns the shared STACBrowserClient instance.
            parent: Parent widget.
        """
        super().__init__("STAC Explorer Settings", parent)
        self.iface = iface
        self._get_stac = get_stac

        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self._settings = QSettings()
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self):
        """Set up the dock widget UI."""
        container = QWidget()
        layout = QVBoxLayout(container)

        tabs = QTabWidget()

        # --- Catalogs tab ---
        catalogs_tab = QWidget()
        catalogs_layout = QVBoxLayout(catalogs_tab)

        catalogs_group = QGroupBox("STAC Catalogs")
        catalogs_group_layout = QVBoxLayout(catalogs_group)

        self.catalog_list = QListWidget()
        catalogs_group_layout.addWidget(self.catalog_list)

        # Add custom catalog
        add_layout = QHBoxLayout()
        self.new_catalog_url_edit = QLineEdit()
        self.new_catalog_url_edit.setPlaceholderText("Custom STAC API URL...")
        add_layout.addWidget(self.new_catalog_url_edit)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_catalog)
        add_layout.addWidget(add_btn)
        catalogs_group_layout.addLayout(add_layout)

        # Catalog action buttons
        catalog_btn_layout = QHBoxLayout()

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_catalog)
        catalog_btn_layout.addWidget(remove_btn)

        set_default_btn = QPushButton("Set as Default")
        set_default_btn.clicked.connect(self._set_default_catalog)
        catalog_btn_layout.addWidget(set_default_btn)

        catalogs_group_layout.addLayout(catalog_btn_layout)

        self.default_catalog_label = QLabel("")
        self.default_catalog_label.setStyleSheet("color: gray; font-size: 10px;")
        self.default_catalog_label.setWordWrap(True)
        catalogs_group_layout.addWidget(self.default_catalog_label)

        catalogs_layout.addWidget(catalogs_group)
        catalogs_layout.addStretch()
        tabs.addTab(catalogs_tab, "Catalogs")

        # --- General tab ---
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_form = QFormLayout()

        self.default_collection_edit = QLineEdit()
        self.default_collection_edit.setPlaceholderText("e.g., sentinel-2-l2a")
        general_form.addRow("Default collection:", self.default_collection_edit)

        self.default_cloud_cover_spin = QSpinBox()
        self.default_cloud_cover_spin.setRange(0, 100)
        self.default_cloud_cover_spin.setValue(30)
        self.default_cloud_cover_spin.setSuffix("%")
        general_form.addRow("Default max cloud cover:", self.default_cloud_cover_spin)

        self.default_max_results_spin = QSpinBox()
        self.default_max_results_spin.setRange(1, 500)
        self.default_max_results_spin.setValue(50)
        general_form.addRow("Default max results:", self.default_max_results_spin)

        self.default_download_dir_edit = QLineEdit()
        self.default_download_dir_edit.setReadOnly(True)
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(self.default_download_dir_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_download_dir)
        dir_layout.addWidget(browse_btn)
        general_form.addRow("Download directory:", dir_layout)

        self.debug_cb = QCheckBox("Enable debug logging")
        general_form.addRow(self.debug_cb)

        general_layout.addLayout(general_form)

        save_general_btn = QPushButton("Save Settings")
        save_general_btn.clicked.connect(self._save_settings)
        general_layout.addWidget(save_general_btn)

        general_layout.addStretch()
        tabs.addTab(general_tab, "General")

        layout.addWidget(tabs)
        self.setWidget(container)

    def _populate_catalog_list(self):
        """Populate the catalog list widget."""
        self.catalog_list.clear()

        default_url = self._settings.value("STACExplorer/default_catalog", "")

        # Add default catalogs
        for name, url in DEFAULT_CATALOGS.items():
            label = f"{name} (built-in)"
            if url == default_url:
                label += " [DEFAULT]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, url)
            item.setData(Qt.UserRole + 1, True)  # is_builtin
            self.catalog_list.addItem(item)

        # Add custom catalogs
        custom_json = self._settings.value("STACExplorer/custom_catalogs", "[]")
        try:
            custom_catalogs = json.loads(custom_json)
        except (json.JSONDecodeError, TypeError):
            custom_catalogs = []

        for entry in custom_catalogs:
            if isinstance(entry, dict):
                url = entry["url"]
                name = entry.get("name", url)
            else:
                url = entry
                name = entry
            label = name
            if url == default_url:
                label += " [DEFAULT]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, url)
            item.setData(Qt.UserRole + 1, False)  # is_builtin
            self.catalog_list.addItem(item)

        if default_url:
            self.default_catalog_label.setText(f"Default: {default_url}")
        else:
            default_url = list(DEFAULT_CATALOGS.values())[0]
            self.default_catalog_label.setText(f"Default: {default_url}")

    def _add_catalog(self):
        """Add a custom catalog URL."""
        url = self.new_catalog_url_edit.text().strip()
        if not url:
            return

        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self,
                "STAC Explorer",
                "Please enter a valid URL starting with http(s).",
            )
            return

        # Check for duplicates
        for i in range(self.catalog_list.count()):
            if self.catalog_list.item(i).data(Qt.UserRole) == url:
                QMessageBox.information(
                    self, "STAC Explorer", "This catalog is already in the list."
                )
                return

        # Add to custom catalogs in settings
        custom_json = self._settings.value("STACExplorer/custom_catalogs", "[]")
        try:
            custom_catalogs = json.loads(custom_json)
        except (json.JSONDecodeError, TypeError):
            custom_catalogs = []

        custom_catalogs.append({"name": url, "url": url})
        self._settings.setValue(
            "STACExplorer/custom_catalogs", json.dumps(custom_catalogs)
        )

        self.new_catalog_url_edit.clear()
        self._populate_catalog_list()

    def _remove_catalog(self):
        """Remove the selected custom catalog."""
        current = self.catalog_list.currentItem()
        if not current:
            QMessageBox.warning(
                self, "STAC Explorer", "Please select a catalog to remove."
            )
            return

        is_builtin = current.data(Qt.UserRole + 1)
        if is_builtin:
            QMessageBox.warning(
                self, "STAC Explorer", "Built-in catalogs cannot be removed."
            )
            return

        url = current.data(Qt.UserRole)

        # Remove from custom catalogs
        custom_json = self._settings.value("STACExplorer/custom_catalogs", "[]")
        try:
            custom_catalogs = json.loads(custom_json)
        except (json.JSONDecodeError, TypeError):
            custom_catalogs = []

        custom_catalogs = [
            c
            for c in custom_catalogs
            if (isinstance(c, dict) and c.get("url") != url)
            or (isinstance(c, str) and c != url)
        ]
        self._settings.setValue(
            "STACExplorer/custom_catalogs", json.dumps(custom_catalogs)
        )

        # Clear default if it was the removed catalog
        if self._settings.value("STACExplorer/default_catalog", "") == url:
            self._settings.setValue("STACExplorer/default_catalog", "")

        self._populate_catalog_list()

    def _set_default_catalog(self):
        """Set the selected catalog as the default."""
        current = self.catalog_list.currentItem()
        if not current:
            QMessageBox.warning(
                self, "STAC Explorer", "Please select a catalog to set as default."
            )
            return

        url = current.data(Qt.UserRole)
        self._settings.setValue("STACExplorer/default_catalog", url)
        self._populate_catalog_list()

        self.iface.messageBar().pushMessage(
            "STAC Explorer",
            f"Default catalog set.",
            level=0,
            duration=3,
        )

    def _browse_download_dir(self):
        """Open a directory browser for the download directory."""
        current_dir = self.default_download_dir_edit.text()
        directory = QFileDialog.getExistingDirectory(
            self, "Select Default Download Directory", current_dir
        )
        if directory:
            self.default_download_dir_edit.setText(directory)

    def _save_settings(self):
        """Save general settings to QSettings."""
        self._settings.setValue(
            "STACExplorer/default_collection",
            self.default_collection_edit.text().strip(),
        )
        self._settings.setValue(
            "STACExplorer/default_cloud_cover",
            self.default_cloud_cover_spin.value(),
        )
        self._settings.setValue(
            "STACExplorer/default_max_results",
            self.default_max_results_spin.value(),
        )
        self._settings.setValue(
            "STACExplorer/default_download_dir",
            self.default_download_dir_edit.text().strip(),
        )
        self._settings.setValue(
            "STACExplorer/debug_mode",
            self.debug_cb.isChecked(),
        )

        QMessageBox.information(self, "STAC Explorer", "Settings saved.")

    def _load_settings(self):
        """Load settings from QSettings."""
        self.default_collection_edit.setText(
            self._settings.value("STACExplorer/default_collection", "")
        )
        self.default_cloud_cover_spin.setValue(
            int(self._settings.value("STACExplorer/default_cloud_cover", 30))
        )
        self.default_max_results_spin.setValue(
            int(self._settings.value("STACExplorer/default_max_results", 50))
        )
        self.default_download_dir_edit.setText(
            self._settings.value("STACExplorer/default_download_dir", "")
        )
        self.debug_cb.setChecked(
            self._settings.value("STACExplorer/debug_mode", False, type=bool)
        )

        self._populate_catalog_list()
