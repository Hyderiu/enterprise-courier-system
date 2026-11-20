"""Read-only CRM reference-data integration."""

from .reference import (
    CrmAPIError,
    CrmReferenceClient,
    replace_local_reference_data_from_crm,
)

__all__ = [
    "CrmAPIError",
    "CrmReferenceClient",
    "replace_local_reference_data_from_crm",
]
