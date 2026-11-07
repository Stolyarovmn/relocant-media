from relocant_media.ops.newsroom_merge import safe_ai_phase


def test_only_fixed_phase_names_can_survive_checkpoint():
    for phase in ("import_sdk", "initialize_client", "sdk_call", "close_client"):
        assert safe_ai_phase(phase) == phase
    for private in ("SECRET", {"phase": "sdk_call"}, None, ["sdk_call"]):
        assert safe_ai_phase(private) is None
