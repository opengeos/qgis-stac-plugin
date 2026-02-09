"""
STAC Explorer Plugin for QGIS

Main plugin class that manages the menu, toolbar, and dock widgets
for searching, visualizing, and downloading STAC catalog data.
"""

import os
import re
import subprocess
import sys

from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QMenu,
    QToolBar,
    QMessageBox,
)
from qgis.core import QgsProject  # noqa: F401
from qgis.PyQt.QtCore import QThread, pyqtSignal


class DependencyInstallWorker(QThread):
    """Worker thread for installing Python package dependencies."""

    finished = pyqtSignal(bool, str)

    def __init__(self, packages):
        """Initialize the dependency installer.

        Args:
            packages: List of package names to install.
        """
        super().__init__()
        self.packages = packages

    def run(self):
        """Install packages using pip."""
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install"] + self.packages,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.finished.emit(True, "Dependencies installed successfully.")
        except subprocess.CalledProcessError as e:
            self.finished.emit(False, f"Failed to install dependencies: {e}")
        except Exception as e:
            self.finished.emit(False, f"Error: {e}")


class STACExplorerPlugin:
    """STAC Explorer Plugin implementation class for QGIS."""

    def __init__(self, iface):
        """Constructor.

        Args:
            iface: An interface instance that provides the hook to QGIS.
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = None
        self.toolbar = None

        # Dock widgets (lazy loaded)
        self._search_dock = None
        self._time_slider_dock = None
        self._time_series_dock = None
        self._settings_dock = None

        # Shared state (lazy loaded)
        self._stac = None

        # Dependency install worker
        self._install_worker = None

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        checkable=False,
        parent=None,
    ):
        """Add a toolbar icon to the toolbar.

        Args:
            icon_path: Path to the icon for this action.
            text: Text that appears in the menu for this action.
            callback: Function to be called when the action is triggered.
            enabled_flag: A flag indicating if the action should be enabled.
            add_to_menu: Flag indicating whether action should be added to menu.
            add_to_toolbar: Flag indicating whether action should be added to toolbar.
            status_tip: Optional text to show in status bar.
            checkable: Whether the action is checkable (toggle).
            parent: Parent widget for the new action.

        Returns:
            The action that was created.
        """
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        action.setCheckable(checkable)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if add_to_toolbar:
            self.toolbar.addAction(action)

        if add_to_menu:
            self.menu.addAction(action)

        self.actions.append(action)

        return action

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        # Create menu
        self.menu = QMenu("&STAC Explorer")
        self.iface.mainWindow().menuBar().addMenu(self.menu)

        # Create toolbar
        self.toolbar = QToolBar("STAC Explorer Toolbar")
        self.toolbar.setObjectName("STACExplorerToolbar")
        self.iface.addToolBar(self.toolbar)

        # Get icon paths with fallbacks
        icon_base = os.path.join(self.plugin_dir, "icons")

        search_icon = os.path.join(icon_base, "search.svg")
        if not os.path.exists(search_icon):
            search_icon = ":/images/themes/default/mActionSearch.svg"

        time_slider_icon = os.path.join(icon_base, "time_slider.svg")
        if not os.path.exists(time_slider_icon):
            time_slider_icon = (
                ":/images/themes/default/mActionTemporalNavigationAnimated.svg"
            )

        time_series_icon = os.path.join(icon_base, "time_series.svg")
        if not os.path.exists(time_series_icon):
            time_series_icon = ":/images/themes/default/mActionAddRasterLayer.svg"

        settings_icon = os.path.join(icon_base, "settings.svg")
        if not os.path.exists(settings_icon):
            settings_icon = ":/images/themes/default/mActionOptions.svg"

        about_icon = os.path.join(icon_base, "about.svg")
        if not os.path.exists(about_icon):
            about_icon = ":/images/themes/default/mActionHelpContents.svg"

        # Search Panel (checkable, toolbar+menu)
        self.search_action = self.add_action(
            search_icon,
            "Search Panel",
            self.toggle_search_dock,
            status_tip="Toggle STAC Search Panel",
            checkable=True,
            parent=self.iface.mainWindow(),
        )

        # Time Slider (checkable, toolbar+menu)
        self.time_slider_action = self.add_action(
            time_slider_icon,
            "Time Slider",
            self.toggle_time_slider_dock,
            status_tip="Toggle Time Slider Panel",
            checkable=True,
            parent=self.iface.mainWindow(),
        )

        # Time Series (checkable, toolbar+menu)
        self.time_series_action = self.add_action(
            time_series_icon,
            "Time Series",
            self.toggle_time_series_dock,
            status_tip="Toggle Time Series Plot Panel",
            checkable=True,
            parent=self.iface.mainWindow(),
        )

        # Settings (checkable, toolbar+menu)
        self.settings_action = self.add_action(
            settings_icon,
            "Settings",
            self.toggle_settings_dock,
            status_tip="Toggle Settings Panel",
            checkable=True,
            parent=self.iface.mainWindow(),
        )

        # Separator
        self.menu.addSeparator()

        # Check for Updates (menu only)
        update_icon = ":/images/themes/default/mActionRefresh.svg"
        self.add_action(
            update_icon,
            "Check for Updates...",
            self.show_update_checker,
            add_to_toolbar=False,
            status_tip="Check for plugin updates from GitHub",
            parent=self.iface.mainWindow(),
        )

        # About (menu only)
        self.add_action(
            about_icon,
            "About STAC Explorer",
            self.show_about,
            add_to_toolbar=False,
            status_tip="About STAC Explorer Plugin",
            parent=self.iface.mainWindow(),
        )

        # Check dependencies on startup
        QTimer.singleShot(500, self._check_dependencies)

    def unload(self):
        """Remove the plugin menu item and icon from QGIS GUI."""
        # Remove dock widgets
        for dock_attr in (
            "_search_dock",
            "_time_slider_dock",
            "_time_series_dock",
            "_settings_dock",
        ):
            dock = getattr(self, dock_attr, None)
            if dock:
                self.iface.removeDockWidget(dock)
                dock.deleteLater()
                setattr(self, dock_attr, None)

        # Remove actions from menu
        for action in self.actions:
            self.iface.removePluginMenu("&STAC Explorer", action)

        # Remove toolbar
        if self.toolbar:
            del self.toolbar

        # Remove menu
        if self.menu:
            self.menu.deleteLater()

    def toggle_search_dock(self):
        """Toggle the Search dock widget visibility."""
        if self._search_dock is None:
            try:
                from .dialogs.search_dock import SearchDockWidget

                self._search_dock = SearchDockWidget(
                    self.iface,
                    self._get_stac,
                    self.load_items_to_time_slider,
                    self.iface.mainWindow(),
                )
                self._search_dock.setObjectName("STACExplorerSearchDock")
                self._search_dock.visibilityChanged.connect(
                    self._on_search_visibility_changed
                )
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self._search_dock)
                self._search_dock.show()
                self._search_dock.raise_()
                return
            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create Search panel:\n{str(e)}",
                )
                self.search_action.setChecked(False)
                return

        if self._search_dock.isVisible():
            self._search_dock.hide()
        else:
            self._search_dock.show()
            self._search_dock.raise_()

    def _on_search_visibility_changed(self, visible):
        """Handle Search dock visibility change.

        Args:
            visible: Whether the dock is now visible.
        """
        self.search_action.setChecked(visible)

    def toggle_time_slider_dock(self):
        """Toggle the Time Slider dock widget visibility."""
        if self._time_slider_dock is None:
            try:
                from .dialogs.time_slider_dock import TimeSliderDockWidget

                self._time_slider_dock = TimeSliderDockWidget(
                    self.iface, self.iface.mainWindow()
                )
                self._time_slider_dock.setObjectName("STACExplorerTimeSliderDock")
                self._time_slider_dock.visibilityChanged.connect(
                    self._on_time_slider_visibility_changed
                )
                self.iface.addDockWidget(
                    Qt.BottomDockWidgetArea, self._time_slider_dock
                )
                self._time_slider_dock.show()
                self._time_slider_dock.raise_()
                return
            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create Time Slider panel:\n{str(e)}",
                )
                self.time_slider_action.setChecked(False)
                return

        if self._time_slider_dock.isVisible():
            self._time_slider_dock.hide()
        else:
            self._time_slider_dock.show()
            self._time_slider_dock.raise_()

    def _on_time_slider_visibility_changed(self, visible):
        """Handle Time Slider dock visibility change.

        Args:
            visible: Whether the dock is now visible.
        """
        self.time_slider_action.setChecked(visible)

    def toggle_time_series_dock(self):
        """Toggle the Time Series dock widget visibility."""
        if self._time_series_dock is None:
            try:
                from .dialogs.time_series_dock import TimeSeriesDockWidget

                self._time_series_dock = TimeSeriesDockWidget(
                    self.iface, self._get_time_steps, self.iface.mainWindow()
                )
                self._time_series_dock.setObjectName("STACExplorerTimeSeriesDock")
                self._time_series_dock.visibilityChanged.connect(
                    self._on_time_series_visibility_changed
                )
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self._time_series_dock)
                self._time_series_dock.show()
                self._time_series_dock.raise_()
                return
            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create Time Series panel:\n{str(e)}",
                )
                self.time_series_action.setChecked(False)
                return

        if self._time_series_dock.isVisible():
            self._time_series_dock.hide()
        else:
            self._time_series_dock.show()
            self._time_series_dock.raise_()

    def _on_time_series_visibility_changed(self, visible):
        """Handle Time Series dock visibility change.

        Args:
            visible: Whether the dock is now visible.
        """
        self.time_series_action.setChecked(visible)

    def toggle_settings_dock(self):
        """Toggle the Settings dock widget visibility."""
        if self._settings_dock is None:
            try:
                from .dialogs.settings_dock import SettingsDockWidget

                self._settings_dock = SettingsDockWidget(
                    self.iface, self._get_stac, self.iface.mainWindow()
                )
                self._settings_dock.setObjectName("STACExplorerSettingsDock")
                self._settings_dock.visibilityChanged.connect(
                    self._on_settings_visibility_changed
                )
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self._settings_dock)
                self._settings_dock.show()
                self._settings_dock.raise_()
                return
            except Exception as e:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Error",
                    f"Failed to create Settings panel:\n{str(e)}",
                )
                self.settings_action.setChecked(False)
                return

        if self._settings_dock.isVisible():
            self._settings_dock.hide()
        else:
            self._settings_dock.show()
            self._settings_dock.raise_()

    def _on_settings_visibility_changed(self, visible):
        """Handle Settings dock visibility change.

        Args:
            visible: Whether the dock is now visible.
        """
        self.settings_action.setChecked(visible)

    def show_about(self):
        """Display the about dialog."""
        version = "Unknown"
        try:
            metadata_path = os.path.join(self.plugin_dir, "metadata.txt")
            with open(metadata_path, "r", encoding="utf-8") as f:
                content = f.read()
                version_match = re.search(r"^version=(.+)$", content, re.MULTILINE)
                if version_match:
                    version = version_match.group(1).strip()
        except Exception:
            pass

        about_text = f"""
<h2>STAC Explorer Plugin for QGIS</h2>
<p>Version: {version}</p>
<p>Author: Qiusheng Wu</p>

<h3>Features:</h3>
<ul>
<li><b>Multi-Catalog Search:</b> Search multiple STAC catalogs with spatial,
    temporal, and cloud cover filters</li>
<li><b>COG Loading:</b> Stream Cloud Optimized GeoTIFFs directly via /vsicurl/</li>
<li><b>Time Slider:</b> Step through multi-temporal raster layers</li>
<li><b>Time Series:</b> Click map locations to plot pixel values over time</li>
<li><b>Download:</b> Download STAC assets to local files</li>
</ul>

<h3>Pre-loaded Catalogs:</h3>
<ul>
<li>Element84 Earth Search</li>
<li>Microsoft Planetary Computer</li>
</ul>

<h3>Links:</h3>
<ul>
<li><a href="https://github.com/opengeos/qgis-stac-plugin">
    GitHub Repository</a></li>
<li><a href="https://github.com/opengeos/qgis-stac-plugin/issues">
    Report Issues</a></li>
</ul>

<p>Licensed under MIT License</p>
"""
        QMessageBox.about(
            self.iface.mainWindow(),
            "About STAC Explorer",
            about_text,
        )

    def show_update_checker(self):
        """Display the update checker dialog."""
        try:
            from .dialogs.update_checker import UpdateCheckerDialog
        except ImportError as e:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Error",
                f"Failed to import update checker dialog:\n{str(e)}",
            )
            return

        try:
            dialog = UpdateCheckerDialog(self.plugin_dir, self.iface.mainWindow())
            dialog.exec_()
        except Exception as e:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Error",
                f"Failed to open update checker:\n{str(e)}",
            )

    def _check_dependencies(self):
        """Check if required Python packages are available."""
        missing = []
        try:
            import pystac_client  # noqa: F401
        except ImportError:
            missing.append("pystac-client")

        if missing:
            msg = (
                f"The following packages are required but not installed:\n"
                f"  {', '.join(missing)}\n\n"
                f"Would you like to install them now?"
            )
            reply = QMessageBox.question(
                self.iface.mainWindow(),
                "STAC Explorer - Missing Dependencies",
                msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self._install_worker = DependencyInstallWorker(missing)
                self._install_worker.finished.connect(self._on_dependencies_installed)
                self._install_worker.start()

    def _on_dependencies_installed(self, success, message):
        """Handle dependency installation result.

        Args:
            success: Whether installation succeeded.
            message: Result message.
        """
        if success:
            QMessageBox.information(
                self.iface.mainWindow(),
                "STAC Explorer",
                f"{message}\n\nPlease restart QGIS to use the plugin.",
            )
        else:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "STAC Explorer",
                f"{message}\n\nYou can install manually:\n"
                f"  pip install pystac-client",
            )

    def _get_stac(self):
        """Get or create the shared STACBrowserClient instance.

        Returns:
            STACBrowserClient instance.
        """
        if self._stac is None:
            from .stac_client import STACBrowserClient

            settings = QSettings()
            default_url = settings.value("STACExplorer/default_catalog", "")
            self._stac = STACBrowserClient(default_url or None)
        return self._stac

    def _get_time_steps(self):
        """Get time steps from the time slider dock.

        Returns:
            List of time step dicts, or empty list if no time slider.
        """
        if self._time_slider_dock is not None:
            return self._time_slider_dock.get_time_steps()
        return []

    def load_items_to_time_slider(self, items, asset_key, render_settings=None):
        """Load STAC items into the time slider dock.

        Opens the time slider dock if not visible, then loads the items.

        Args:
            items: List of STAC item dicts from STACBrowserClient.search().
            asset_key: Asset key to use for COG URLs (e.g., "visual").
            render_settings: Optional dict with render mode and parameters.
        """
        # Ensure time slider dock exists and is visible
        if self._time_slider_dock is None:
            self.toggle_time_slider_dock()
        elif not self._time_slider_dock.isVisible():
            self._time_slider_dock.show()
            self._time_slider_dock.raise_()

        if self._time_slider_dock:
            self._time_slider_dock.load_items(items, asset_key, render_settings)
