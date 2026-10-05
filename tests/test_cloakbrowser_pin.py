"""Guard the cloakbrowser pin without launching a browser."""

import importlib
import inspect
from types import ModuleType

cloakbrowser: ModuleType = importlib.import_module("cloakbrowser")


def test_exports_license_error() -> None:
    """The licence error class must be exported by the installed cloakbrowser."""
    assert hasattr(cloakbrowser, "CloakBrowserLicenseError")
    assert issubclass(cloakbrowser.CloakBrowserLicenseError, Exception)


def test_launch_accepts_license_key() -> None:
    """launch() must accept a license_key parameter."""
    parameters = inspect.signature(cloakbrowser.launch).parameters
    assert "license_key" in parameters
