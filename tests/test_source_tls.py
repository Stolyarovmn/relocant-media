"""Network-free checks for the source TLS context and bundled intermediates."""

import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
import httpx
import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from relocant_media.collectors.tls import (
    SOURCE_INTERMEDIATES,
    build_ssl_context,
    is_missing_intermediate,
    source_ssl_context,
)
from relocant_media.ops.source_audit import render_source_health_markdown

_NOW = datetime.now(UTC)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _cert(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: str,
    issuer_key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
    san: str | None = None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - timedelta(days=1))
        .not_valid_after(_NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Root -> intermediate -> leaf(localhost); the server sends only the leaf."""
    base = tmp_path_factory.mktemp("pki")
    root_key, mid_key, leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _cert("Test Root", root_key, "Test Root", root_key, ca=True)
    mid = _cert("Test Intermediate", mid_key, "Test Root", root_key, ca=True)
    leaf = _cert("localhost", leaf_key, "Test Intermediate", mid_key, ca=False, san="localhost")
    paths = {
        "root": base / "root.pem",
        "mid": base / "mid.pem",
        "leaf": base / "leaf.pem",
        "key": base / "leaf.key",
    }
    paths["root"].write_bytes(_pem(root))
    paths["mid"].write_bytes(_pem(mid))
    paths["leaf"].write_bytes(_pem(leaf))
    paths["key"].write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


@pytest.fixture(scope="module")
def leaf_only_server(pki: dict[str, Path]):  # type: ignore[no-untyped-def]
    """A local TLS server that, like the broken sites, omits its intermediate."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(pki["leaf"], pki["key"])
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with server_context.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield port
    stop.set()
    thread.join(timeout=2)
    listener.close()


def _handshake(context: ssl.SSLContext, port: int, hostname: str = "localhost") -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=hostname):
            pass


def test_leaf_only_server_fails_without_intermediate(
    pki: dict[str, Path], leaf_only_server: int,
) -> None:
    context = build_ssl_context(roots=pki["root"], intermediates=None)
    with pytest.raises(ssl.SSLCertVerificationError) as raised:
        _handshake(context, leaf_only_server)
    assert is_missing_intermediate(raised.value)


def test_bundled_intermediate_completes_chain(
    pki: dict[str, Path], leaf_only_server: int,
) -> None:
    _handshake(build_ssl_context(roots=pki["root"], intermediates=pki["mid"]), leaf_only_server)


def test_hostname_is_still_checked(pki: dict[str, Path], leaf_only_server: int) -> None:
    context = build_ssl_context(roots=pki["root"], intermediates=pki["mid"])
    with pytest.raises(ssl.SSLCertVerificationError) as raised:
        _handshake(context, leaf_only_server, hostname="example.org")
    assert not is_missing_intermediate(raised.value)


def test_intermediate_alone_is_not_a_trust_anchor(
    pki: dict[str, Path], leaf_only_server: int,
) -> None:
    """Without the matching root the helper intermediate must not be enough."""
    context = build_ssl_context(roots=certifi.where(), intermediates=pki["mid"])
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(context, leaf_only_server)


def test_source_context_verifies_by_default() -> None:
    context = source_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert not context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def _bundle() -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(SOURCE_INTERMEDIATES.read_bytes())


@pytest.mark.filterwarnings("ignore:Parsed a serial number")
def test_bundled_intermediates_chain_to_certifi_and_stay_valid() -> None:
    roots = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
    bundle = _bundle()
    assert {c.subject.rfc4514_string() for c in bundle} == {
        "CN=GlobalSign RSA OV SSL CA 2018,O=GlobalSign nv-sa,C=BE",
        "CN=Sectigo Public Server Authentication CA OV R36,O=Sectigo Limited,C=GB",
    }
    for cert in bundle:
        assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        assert cert.issuer != cert.subject, "roots belong in certifi, not in this bundle"
        assert cert.not_valid_after_utc > _NOW + timedelta(days=30), cert.subject
        issuers = [root for root in roots if root.subject == cert.issuer]
        assert issuers, f"no certifi root for {cert.subject.rfc4514_string()}"
        verified = False
        for root in issuers:
            try:
                cert.verify_directly_issued_by(root)
                verified = True
            except (InvalidSignature, ValueError, TypeError):
                continue
        assert verified, cert.subject


def test_missing_intermediate_detected_through_httpx_wrapping() -> None:
    cause = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1011)",
    )
    cause.verify_code = 20
    try:
        try:
            raise cause
        except ssl.SSLError as inner:
            raise httpx.ConnectError(str(inner)) from inner
    except httpx.ConnectError as wrapped:
        assert is_missing_intermediate(wrapped)
    assert not is_missing_intermediate(httpx.ConnectError("connection refused"))


def test_summary_shows_missing_intermediate_hint() -> None:
    rendered = render_source_health_markdown({
        "sources": [{
            "id": "srbijavoz_news", "status": "transport_error", "attempts": 3,
            "error_type": "ConnectError", "error_message": "certificate verify failed",
            "tls_hint": "server did not send its intermediate certificate",
        }],
    })
    assert "server did not send its intermediate certificate" in rendered
