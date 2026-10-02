"""Certificate validation and atomic active-pair replacement without network I/O."""

from pathlib import Path

import pytest

from api import tls_bootstrap as tls


def test_explicit_addresses_and_matching_pair():
    address, cert, key = tls.ensure_cert(["localhost", "127.0.0.1", "::1"])
    assert address == "localhost"
    from cryptography import x509

    certificate = x509.load_pem_x509_certificate(Path(cert).read_bytes())
    assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert tls.ensure_cert(["localhost"], cert, key) == (address, cert, key)
    _, other_cert, other_key = tls.ensure_cert(["game.example"])
    assert other_cert != cert
    with pytest.raises(ValueError, match="do not match"):
        tls.ensure_cert(["localhost"], cert, other_key)
    with pytest.raises(ValueError, match="does not cover"):
        tls.ensure_cert(["other.example"], cert, key)


def test_failed_pair_publication_keeps_previous_pair(monkeypatch):
    first = tls.ensure_cert(["localhost"])
    pointer = tls.CERT_DIR / "active.json"
    before = pointer.read_bytes()
    replace = Path.replace

    def fail_pointer(path, target):
        if Path(target).name == "active.json":
            raise OSError("injected disk failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_pointer)
    with pytest.raises(OSError, match="injected"):
        tls.ensure_cert(["game.example"])
    assert pointer.read_bytes() == before
    assert tls.ensure_cert(["localhost"]) == first
    assert len(list(tls.CERT_DIR.glob("pair-*"))) == 1


def test_corrupt_generated_pair_recovers_but_supplied_pair_is_untouched():
    _, cert, key = tls.ensure_cert(["localhost"])
    Path(key).write_bytes(b"invalid key")
    with pytest.raises(ValueError):
        tls.ensure_cert(["localhost"], cert, key)
    assert Path(key).read_bytes() == b"invalid key"
    assert tls.ensure_cert(["localhost"])[2] != key


def test_offline_address_fallback(monkeypatch):
    import urllib.request

    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(tls.socket, "socket", unavailable)
    monkeypatch.setattr(tls.socket, "gethostbyname", unavailable)
    assert tls.get_external_ip() == "127.0.0.1"
