"""TLS settings for fetching public sources.

Some official Serbian sites (srbijavoz.rs, parking-servis.co.rs) send only their
leaf certificate and omit the intermediate. Browsers fetch the missing issuer via
AIA, Python does not, so verification fails with "unable to get local issuer
certificate". We ship the missing *public intermediates* and add them to the
verify store next to certifi's roots. Verification is never relaxed: the chain
must still end at a certifi root (partial chains are refused), and hostname
checking stays on.
"""

import ssl
from functools import cache
from pathlib import Path

import certifi

SOURCE_INTERMEDIATES = Path(__file__).with_name("ca") / "source_intermediates.pem"
MISSING_INTERMEDIATE_HINT = (
    "server did not send its intermediate certificate; add the issuer to "
    "collectors/ca/source_intermediates.pem after verifying it against certifi"
)
# OpenSSL X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY.
_UNABLE_TO_GET_ISSUER_LOCALLY = 20


def build_ssl_context(
    roots: str | Path | None = None,
    intermediates: str | Path | None = SOURCE_INTERMEDIATES,
) -> ssl.SSLContext:
    """Return a verifying client context: trusted roots plus helper intermediates."""
    context = ssl.create_default_context(cafile=str(roots or certifi.where()))
    if intermediates is not None:
        context.load_verify_locations(cafile=str(intermediates))
    # Helper intermediates must never become trust anchors on their own.
    context.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("source TLS context must verify certificates and hostnames")
    return context


@cache
def source_ssl_context() -> ssl.SSLContext:
    """Shared context for every public-source HTTP client."""
    return build_ssl_context()


def is_missing_intermediate(error: BaseException) -> bool:
    """True when a (possibly wrapped) error is the missing-issuer TLS failure."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return current.verify_code == _UNABLE_TO_GET_ISSUER_LOCALLY
        if "unable to get local issuer certificate" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False
