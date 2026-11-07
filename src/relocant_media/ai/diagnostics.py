"""Fixed, non-content failure coordinates shared by worker and projection."""
AI_PHASES = frozenset({
    "import_sdk", "initialize_client", "sdk_call", "close_client",
    "http_request", "http_connect", "http_tls", "http_send",
    "http_wait_headers", "http_read_body", "http_body_received",
    "http_error_429", "http_error_4xx", "http_error_5xx",
})
