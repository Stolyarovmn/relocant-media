from relocant_media.services.analytics import PostSnapshot, effective_cpl, engagement_per_view


def test_engagement_per_view() -> None:
    snapshot = PostSnapshot(views=1000, reactions=50, comments=20, forwards=30)
    assert engagement_per_view(snapshot) == 0.1


def test_effective_cpl() -> None:
    assert effective_cpl(35.0, 80) == 0.4375
    assert effective_cpl(10.0, 0) is None
