"""Exact website, runner, and PGL compatibility contract."""

from . import __version__
from ._pgl_pin import PGL_COMMIT


CONTRACT_REVISION = "dbp-pgl-integration-v2"
PGL_INTEGRATION_REVISION = "dbp-prepared-block-v2"


def expected_compatibility():
    return {
        "contract_revision": CONTRACT_REVISION,
        "runner_version": __version__,
        "pgl_commit": PGL_COMMIT,
        "pgl_integration_revision": PGL_INTEGRATION_REVISION,
    }


def validate_compatibility(value):
    if type(value) is not dict or value != expected_compatibility():
        from .models import ContractError
        raise ContractError(
            "Study requires a different DBP runner or PGL build; "
            "install the exact published versions"
        )
    return dict(value)
