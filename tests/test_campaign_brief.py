from types import SimpleNamespace
from typing import Any

import pytest

from relocant_media.ops.campaign_brief import generate_proposal


class FakeInteractions:
    def __init__(self, output: object) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output)


def test_shared_brief_is_sent_once_without_storing_interaction() -> None:
    client = FakeInteractions("Предложение")
    assert generate_proposal(client, model="configured-model", brief="Бриф") == "Предложение"
    assert client.calls == [{"model": "configured-model", "input": "Бриф", "store": False}]


@pytest.mark.parametrize("output", [None, "", "   ", 12])
def test_missing_proposal_is_a_failure(output: object) -> None:
    with pytest.raises(ValueError, match="no proposal"):
        generate_proposal(FakeInteractions(output), model="configured-model", brief="Бриф")


def test_missing_model_does_not_call_api() -> None:
    client = FakeInteractions("unused")
    with pytest.raises(ValueError, match="required"):
        generate_proposal(client, model="", brief="Бриф")
    assert client.calls == []
