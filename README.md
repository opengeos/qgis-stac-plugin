# STAC Explorer - QGIS Plugin

A QGIS plugin for searching, visualizing, and downloading data from multiple STAC (SpatioTemporal Asset Catalog) catalogs.

## Features

- **Multi-Catalog Support**: Pre-loaded catalogs (Element84 Earth Search, Microsoft Planetary Computer) plus custom user-provided catalog URLs
- **Collection Browsing**: Browse and search collections with asset key inspection
- **Search Filters**: Spatial (bounding box), temporal (date range), and cloud cover filters
- **COG Streaming**: Stream Cloud Optimized GeoTIFFs directly via `/vsicurl/`
- **Time Slider**: Step through multi-temporal raster layers with auto-play
- **Time Series**: Click map locations to plot pixel values over time with matplotlib
- **Asset Download**: Download STAC assets to local files
- **Render Options**: Single band (colormap) or RGB composite rendering

## Installation

### From Source

```bash
# Clone the repository
git clone https://github.com/opengeos/qgis-stac-plugin.git
cd qgis-stac-plugin

# Install to QGIS plugins directory
python install.py

# Or use the shell script
./install.sh
```

### Dependencies

The plugin requires `pystac-client`. It will prompt to install it automatically on first use.

Optional dependencies:
- `planetary-computer`: For automatic item signing with Microsoft Planetary Computer
- `matplotlib`: For time series plotting

## Usage

1. Enable the plugin in QGIS: **Plugins > Manage and Install Plugins > STAC Explorer**
2. Open the **Search Panel** from the toolbar or menu
3. Select a catalog and collection
4. Set spatial/temporal filters and click **Search**
5. Select results and **Load Selected** to add layers or **Download Selected** to save locally

## Development

### Linting

```bash
pre-commit run --all-files
```

### Packaging

```bash
python package_plugin.py
```

## License

MIT License - see [LICENSE](LICENSE) for details.
