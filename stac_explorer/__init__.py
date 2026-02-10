"""
STAC Explorer QGIS Plugin

A plugin for searching, visualizing, and downloading data from
multiple STAC (SpatioTemporal Asset Catalog) catalogs.
"""


def classFactory(iface):
    """Load the STAC Explorer plugin.

    Args:
        iface: A QGIS interface instance.

    Returns:
        STACExplorerPlugin instance.
    """
    from .stac_explorer_plugin import STACExplorerPlugin

    return STACExplorerPlugin(iface)
