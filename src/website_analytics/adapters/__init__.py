"""Read-only adapters for registered website analytics data sources."""

from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.adapters.gsc import GSCAdapter

__all__ = ["GA4Adapter", "GSCAdapter"]
