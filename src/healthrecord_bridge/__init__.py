"""healthrecord-bridge: convert personal health record exports into FHIR R4."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("healthrecord-bridge")
except PackageNotFoundError:  # imported from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
