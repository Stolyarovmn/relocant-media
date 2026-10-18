from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PostSnapshot:
    views: int
    reactions: int
    comments: int
    forwards: int


def engagement_per_view(snapshot: PostSnapshot) -> float:
    if snapshot.views <= 0:
        return 0.0
    interactions = snapshot.reactions + snapshot.comments + snapshot.forwards
    return interactions / snapshot.views


def effective_cpl(spend_eur: float, retained_subscribers: int) -> float | None:
    if retained_subscribers <= 0:
        return None
    return spend_eur / retained_subscribers
