"""Tests for web/build_site.py: downloads are only used when their checksum matches."""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_site as build  # noqa: E402


def test_fetch_checks_sha256(tmp_path):
    source = tmp_path / "file.bin"
    source.write_bytes(b"pyodide")
    good = hashlib.sha256(b"pyodide").hexdigest()
    cache = tmp_path / "cache"
    assert build.fetch(source.as_uri(), good, cache).read_bytes() == b"pyodide"
    with pytest.raises(SystemExit, match="checksum"):
        build.fetch(source.as_uri(), "0" * 64, tmp_path / "other")


def test_fetch_rejects_tampered_cache(tmp_path):
    source = tmp_path / "file.bin"
    source.write_bytes(b"pyodide")
    good = hashlib.sha256(b"pyodide").hexdigest()
    cache = tmp_path / "cache"
    build.fetch(source.as_uri(), good, cache).write_bytes(b"evil")
    with pytest.raises(SystemExit, match="checksum"):
        build.fetch(source.as_uri(), good, cache)
