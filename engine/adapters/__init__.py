"""The catalogs the engine can run, keyed by the ACTIVE_DOMAIN value that selects each one."""
from engine.adapters.cars import CarsAdapter
from engine.adapters.real_estate import RealEstateAdapter
from engine.core.adapter_base import DomainAdapter

ADAPTERS: dict[str, type[DomainAdapter]] = {
    adapter.domain_type: adapter for adapter in (CarsAdapter, RealEstateAdapter)
}
