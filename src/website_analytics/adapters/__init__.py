"""Read-only adapters for registered website analytics data sources."""

from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.adapters.gsc import GSCAdapter, GSCQueryResult
from website_analytics.adapters.page_dimension import LegacyPageDimensionAdapter

__all__ = ["GA4Adapter", "GSCAdapter", "GSCQueryResult", "LegacyPageDimensionAdapter"]
