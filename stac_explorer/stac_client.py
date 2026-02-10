"""
STAC Browser Client Module

Multi-catalog STAC API wrapper with client caching. Returns plain dicts
for safe cross-thread use. Supports Element84 Earth Search, Microsoft
Planetary Computer, and custom user-provided catalog URLs.
"""

import logging
from datetime import datetime, timezone

try:
    from pystac_client import Client
except ImportError:
    Client = None

try:
    import planetary_computer as pc

    HAS_PC = True
except ImportError:
    HAS_PC = False

DEFAULT_CATALOGS = {
    "Element84 Earth Search": "https://earth-search.aws.element84.com/v1",
    "Microsoft Planetary Computer": (
        "https://planetarycomputer.microsoft.com/api/stac/v1"
    ),
}

_PC_HOST = "planetarycomputer.microsoft.com"

_logger = logging.getLogger(__name__)


class STACBrowserClient:
    """Multi-catalog client for STAC APIs."""

    def __init__(self, stac_url=None):
        """Initialize the STAC client.

        Args:
            stac_url: Initial STAC API URL. Defaults to Element84 Earth Search.
        """
        default_url = list(DEFAULT_CATALOGS.values())[0]
        self._stac_url = stac_url or default_url
        self._client_cache = {}

    def set_catalog(self, url):
        """Switch to a different STAC catalog.

        Args:
            url: STAC API URL to switch to.
        """
        self._stac_url = url

    def get_catalog_url(self):
        """Get the current catalog URL.

        Returns:
            Current STAC API URL string.
        """
        return self._stac_url

    def _get_client(self):
        """Get or create a cached pystac_client Client instance.

        Returns:
            pystac_client.Client instance for the current catalog URL.

        Raises:
            ImportError: If pystac_client is not installed.
        """
        if Client is None:
            raise ImportError("pystac-client is required: pip install pystac-client")
        if self._stac_url not in self._client_cache:
            self._client_cache[self._stac_url] = Client.open(self._stac_url)
        return self._client_cache[self._stac_url]

    def _is_pc_catalog(self):
        """Check if the current catalog is Microsoft Planetary Computer.

        Returns:
            True if the current catalog URL is a Planetary Computer URL.
        """
        return _PC_HOST in self._stac_url

    def _sign_item(self, item):
        """Sign a STAC item if using Planetary Computer and package is available.

        Args:
            item: pystac Item to sign.

        Returns:
            Signed item, or original item if signing is not available.
        """
        if HAS_PC and self._is_pc_catalog():
            try:
                return pc.sign(item)
            except Exception as e:
                _logger.debug("Failed to sign item %s: %s", item.id, e)
        return item

    def get_collections(self):
        """List all available collections from the current catalog.

        Returns:
            List of dicts with 'id', 'title', and 'description' keys.
        """
        client = self._get_client()
        collections = []
        for c in client.get_collections():
            collections.append(
                {
                    "id": c.id,
                    "title": getattr(c, "title", c.id) or c.id,
                    "description": getattr(c, "description", "") or "",
                }
            )
        return collections

    def search(
        self,
        collections,
        bbox=None,
        datetime_range=None,
        max_cloud_cover=None,
        limit=100,
        unique_dates=True,
    ):
        """Search for items in the current STAC catalog.

        Args:
            collections: List of collection IDs to search.
            bbox: Bounding box [west, south, east, north] in WGS84.
            datetime_range: Tuple of (start, end) as strings "YYYY-MM-DD"
                or datetime objects.
            max_cloud_cover: Maximum cloud cover percentage (0-100).
            limit: Maximum number of items to return.
            unique_dates: If True, return only one item per unique date.

        Returns:
            List of dicts with item metadata, sorted by date. Each dict
            contains 'id', 'datetime', 'date_str', 'cloud_cover', and
            'assets' keys.
        """
        client = self._get_client()

        search_kwargs = {"collections": collections}

        if bbox:
            search_kwargs["bbox"] = bbox

        if datetime_range:
            start, end = datetime_range
            if isinstance(start, str):
                start = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            if isinstance(end, str):
                end = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
            search_kwargs["datetime"] = [start, end]

        if max_cloud_cover is not None:
            try:
                search_kwargs["filter"] = {
                    "op": "<=",
                    "args": [
                        {"property": "eo:cloud_cover"},
                        max_cloud_cover,
                    ],
                }
                search_kwargs["filter_lang"] = "cql2-json"
            except Exception:
                _logger.debug("CQL2 filter not supported, will filter client-side")

        if limit:
            search_kwargs["limit"] = limit
            if not unique_dates:
                search_kwargs["max_items"] = limit

        try:
            search_result = client.search(**search_kwargs)
            items_iter = search_result.items()
        except Exception:
            # If CQL2 filter is rejected by the API, fall back to unfiltered search
            if "filter" in search_kwargs:
                _logger.debug(
                    "CQL2 filter rejected by API, falling back to client-side filtering"
                )
                search_kwargs.pop("filter")
                search_kwargs.pop("filter_lang", None)
                search_result = client.search(**search_kwargs)
                items_iter = search_result.items()
            else:
                raise

        # Convert to plain dicts for thread safety.
        result = []
        seen_dates = set()
        for item in items_iter:
            item = self._sign_item(item)

            # Client-side cloud cover filtering (always applied as a safeguard)
            if max_cloud_cover is not None:
                cc = item.properties.get("eo:cloud_cover")
                if cc is not None and cc > max_cloud_cover:
                    continue

            date_str = item.datetime.strftime("%Y-%m-%d")

            if unique_dates:
                if date_str in seen_dates:
                    continue
                seen_dates.add(date_str)

            cloud_cover = item.properties.get("eo:cloud_cover")
            assets = {}
            for key, asset in item.assets.items():
                assets[key] = {
                    "href": asset.href,
                    "type": getattr(asset, "media_type", None),
                    "title": getattr(asset, "title", key),
                }

            result.append(
                {
                    "id": item.id,
                    "datetime": item.datetime.isoformat(),
                    "date_str": date_str,
                    "cloud_cover": cloud_cover,
                    "geometry": item.geometry,
                    "bbox": list(item.bbox) if item.bbox else None,
                    "assets": assets,
                }
            )

            if limit and len(result) >= limit:
                break

        # Sort by date
        result.sort(key=lambda i: i["datetime"])

        return result

    def get_collection_asset_keys(self, collection_id):
        """Get available asset keys for a collection.

        Inspects the first item in the collection to determine which
        asset keys are available (e.g., "visual", "B04", "SCL").

        Args:
            collection_id: The collection ID to inspect.

        Returns:
            List of asset key strings.
        """
        client = self._get_client()

        search_result = client.search(collections=[collection_id], limit=1, max_items=1)
        items = list(search_result.items())

        if not items:
            return []

        return list(items[0].assets.keys())
