from __future__ import annotations

import pytest

from website_analytics.windows_credentials import (
    WindowsCredentialError,
    delete_windows_generic_credential,
    load_windows_generic_credential,
    store_windows_generic_credential,
)


def test_windows_credential_operations_reject_non_analytics_targets_before_os_access() -> None:
    with pytest.raises(WindowsCredentialError, match="target"):
        load_windows_generic_credential("OtherApp/credential")
    with pytest.raises(WindowsCredentialError, match="target"):
        store_windows_generic_credential("OtherApp/credential", "secret")
    with pytest.raises(WindowsCredentialError, match="target"):
        delete_windows_generic_credential("OtherApp/credential")
