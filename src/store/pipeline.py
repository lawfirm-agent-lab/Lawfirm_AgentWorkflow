"""Pipeline storage: legacy tables plus versioned migrations (see sql/migrations)."""
from src.store.db import Store
from src.store.migrate import apply_migrations


class PipelineStore(Store):
    def _initialize(self):
        super()._initialize()
        apply_migrations(self)
