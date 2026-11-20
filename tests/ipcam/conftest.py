"""IP camera tests: disposable data, no keyring, no real devices."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "usr/share/biglinux/bigcam"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("BIGCAM_DISABLE_KEYRING", "1")
    from core.ipcam.security import CredentialStore
    monkeypatch.setattr(CredentialStore, "_instance", None)
