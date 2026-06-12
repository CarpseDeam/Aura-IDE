from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactType:
    name: str
    schema: dict[str, str]  # field name → type string: "string"|"number"|"bool"|"list"|"object"|"any"
    description: str = ""


# ── Built-in registry ──────────────────────────────────────────────

SearchBrief = ArtifactType(
    name="SearchBrief",
    schema={"query": "string", "domain": "string"},
    description="A search query with target domain.",
)

OpportunityBatch = ArtifactType(
    name="OpportunityBatch",
    schema={"opportunities": "list", "source": "string"},
    description="A batch of opportunities found via search.",
)

FitReview = ArtifactType(
    name="FitReview",
    schema={"opportunity_id": "string", "fit_score": "number", "reasoning": "string"},
    description="Fit assessment for one opportunity.",
)

ReplyDrafts = ArtifactType(
    name="ReplyDrafts",
    schema={"drafts": "list", "opportunity_id": "string"},
    description="Drafted replies for an opportunity.",
)

PostingLog = ArtifactType(
    name="PostingLog",
    schema={"action": "string", "result": "string", "timestamp": "string"},
    description="A record of an action taken.",
)

BUILTIN_TYPES: dict[str, ArtifactType] = {
    t.name: t
    for t in [SearchBrief, OpportunityBatch, FitReview, ReplyDrafts, PostingLog]
}


# ── Contract validation ────────────────────────────────────────────


def is_compatible(producer: ArtifactType, consumer: ArtifactType) -> bool:
    """Returns True when every field the consumer's schema *requires* is present
    in the producer's schema AND the type string matches.

    Producer may have extra fields (width is fine).  An ``"any"`` field matches
    any type including ``"any"``.  A consumer field of type ``"any"`` accepts
    anything.
    """
    for field_name, consumer_type in consumer.schema.items():
        producer_type = producer.schema.get(field_name)
        if producer_type is None:
            return False
        if consumer_type == "any":
            continue
        if producer_type == "any":
            continue
        if producer_type != consumer_type:
            return False
    return True


TYPE_NAMES: list[str] = sorted(BUILTIN_TYPES.keys())
