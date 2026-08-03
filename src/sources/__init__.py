"""Source registry.

Two families, both behind one interface:

  * DataProvider — facts (calendar, detail strip, subscription, listing outcomes).
    Tried in the order set by config.DATA_PROVIDERS until one returns data.
  * Source — opinions, turned into SourceCalls and scored in the accuracy ledger.

Adding either is one new file plus one line in the relevant registry below.
"""

from __future__ import annotations

from ..config import DATA_PROVIDERS, SOURCES_ENABLED
from .base import DataProvider, Source
from .brokers import BrokersSource
from .chittorgarh import ChittorgarhSource
from .expert import NamedExpertSource
from .gmp import GMPSource
from .ipowatch import IPOWatchSource
from .nse_subscription import NSESubscriptionSource
from .qib_signal import QIBSignalSource
from .sandeep_jain import SandeepJainSource
from .singhvi import SinghviSource

PROVIDER_REGISTRY: dict[str, type[DataProvider]] = {
    "ipowatch": IPOWatchSource,
    "chittorgarh": ChittorgarhSource,
    "nse_subscription": NSESubscriptionSource,
}

REGISTRY: dict[str, type[Source]] = {
    "gmp": GMPSource,
    "qib_signal": QIBSignalSource,
    "singhvi": SinghviSource,
    "sandeep_jain": SandeepJainSource,
    "brokers": BrokersSource,
}


def build_sources() -> list[Source]:
    """Instantiate every enabled opinion source, in config order."""
    return [REGISTRY[key]() for key in SOURCES_ENABLED if key in REGISTRY]


def build_providers() -> dict[str, DataProvider]:
    """One instance per provider name mentioned anywhere in DATA_PROVIDERS."""
    names: list[str] = []
    for chain in DATA_PROVIDERS.values():
        for name in chain:
            if name not in names and name in PROVIDER_REGISTRY:
                names.append(name)
    return {name: PROVIDER_REGISTRY[name]() for name in names}


def provider_chain(role: str, providers: dict[str, DataProvider]) -> list[DataProvider]:
    """The providers to try, in order, for one role ('calendar', 'subscription', ...)."""
    return [providers[name] for name in DATA_PROVIDERS.get(role, []) if name in providers]


__all__ = [
    "DataProvider",
    "NamedExpertSource",
    "PROVIDER_REGISTRY",
    "REGISTRY",
    "Source",
    "build_providers",
    "build_sources",
    "provider_chain",
    "BrokersSource",
    "ChittorgarhSource",
    "GMPSource",
    "IPOWatchSource",
    "NSESubscriptionSource",
    "QIBSignalSource",
    "SandeepJainSource",
    "SinghviSource",
]
