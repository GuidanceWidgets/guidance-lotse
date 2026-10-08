"""This is used to define the HTTP contracts in Lotse that are shared with GuidanceWidgets."""

from typing import Any, Dict, List, Optional, Union

import pydantic
from pydantic import BaseModel, Field
from typing_extensions import Literal


PYDANTIC_V2 = int(pydantic.VERSION.split(".", 1)[0]) >= 2
if PYDANTIC_V2:
    from pydantic import ConfigDict, model_validator
else:
    from pydantic import root_validator

# Define the state used in guidance widgets.
GuidanceStateValue = Literal["past", "present", "problem", "future"]
# Define the guidance levels used in guidance widgets.
GuidanceLevelValue = Literal[0, 1, 2, 3]
# Use union to choose either a fixed level or an adaptive level for guidance widgets.
RequestedLevelValue = Union[GuidanceLevelValue, Literal["adapt"]]

# Allow the creation of all contract models, with configuration for Pydantic v1 and v2.
class ContractModel(BaseModel):
    if PYDANTIC_V2:
        model_config = ConfigDict(populate_by_name=True, extra="forbid")
    else:
        class Config:
            allow_population_by_field_name = True
            extra = "forbid"

# Define the record for a provenance entry.
class ProvenanceRecord(ContractModel):
    value: Any
    timestamp: str
    source: str
    kind: str
    caller: Optional[Union[str, int]] = None

# Define the record for a widget entry.
class ProvenanceSnapshot(ContractModel):
    widget_id: str = Field(alias="widgetId", min_length=1)
    widget_type: str = Field(alias="widgetType", min_length=1)
    mode: str
    sample_interval_ms: int = Field(alias="sampleIntervalMs", ge=0)
    data: List[ProvenanceRecord]


class InteractionSequenceRecord(ContractModel):
    sequence: int = Field(ge=1)
    widget_id: str = Field(alias="widgetId", min_length=1)
    record_index: int = Field(alias="recordIndex", ge=0)


class StateSnapshot(ContractModel):
    widgets: List[ProvenanceSnapshot]
    interaction_sequence: List[InteractionSequenceRecord] = Field(
        default_factory=list,
        alias="interactionSequence",
    )
    application_context: Dict[str, Any] = Field(
        default_factory=dict,
        alias="applicationContext",
    )

    if PYDANTIC_V2:
        @model_validator(mode="after")
        def validate_snapshot(self):
            _validate_snapshot(self.widgets, self.interaction_sequence)
            return self
    else:
        @root_validator
        def validate_snapshot(cls, values):
            _validate_snapshot(
                values.get("widgets", []),
                values.get("interaction_sequence", []),
            )
            return values

# This is used to validate the only ID for widgets.
def _validate_snapshot(widgets, sequence):
    widget_ids = [widget.widget_id for widget in widgets]
    if len(widget_ids) != len(set(widget_ids)):
        raise ValueError("widgetId values must be unique")

    widgets_by_id = {widget.widget_id: widget for widget in widgets}
    if [item.sequence for item in sequence] != list(
        range(1, len(sequence) + 1)
    ):
        raise ValueError("interactionSequence must be contiguous")

    for item in sequence:
        widget = widgets_by_id.get(item.widget_id)
        if widget is None:
            raise ValueError(
                "interactionSequence references an unknown widgetId"
            )
        if item.record_index >= len(widget.data):
            raise ValueError(
                "interactionSequence recordIndex is outside widget history"
            )
        if widget.data[item.record_index].kind != "interaction":
            raise ValueError(
                "interactionSequence must reference interaction records"
            )


class SessionState(StateSnapshot):
    session_id: str
    revision: int = Field(ge=0)
    observed_at: Optional[str] = Field(default=None, alias="observedAt")


class GuidanceSummary(ContractModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    attributes: List[Dict[str, Any]] = Field(default_factory=list)
    message: Optional[str] = None


class GuidancePresentation(ContractModel):
    orienting: Optional[GuidanceSummary] = None
    directing: Optional[GuidanceSummary] = None
    prescribing: Optional[GuidanceSummary] = None


class GuidanceAction(BaseModel):
    kind: str = Field(min_length=1)
    target_widget_id: str = Field(alias="targetWidgetId", min_length=1)

    if PYDANTIC_V2:
        model_config = ConfigDict(populate_by_name=True, extra="allow")
    else:
        class Config:
            allow_population_by_field_name = True
            extra = "allow"


class GuidanceCandidate(ContractModel):
    candidate_id: str = Field(alias="candidateId", min_length=1)
    strategy_id: str = Field(alias="strategyId", min_length=1)
    guidance_topic_id: str = Field(alias="guidanceTopicId", min_length=1)
    based_on_state_revision: int = Field(alias="basedOnStateRevision", ge=0)
    state: GuidanceStateValue
    priority: float = 0
    level: Optional[GuidanceLevelValue] = None
    max_level: Optional[GuidanceLevelValue] = Field(
        default=None,
        alias="maxLevel",
    )
    summary: Optional[GuidanceSummary] = None
    presentation: Optional[GuidancePresentation] = None
    actions: List[GuidanceAction] = Field(min_items=1)
    source_problem: Optional[str] = Field(default=None, alias="sourceProblem")
    evidence: Dict[str, Any] = Field(default_factory=dict)
    options_count: Optional[int] = Field(default=None, alias="optionsCount")


class PolicyDecision(ContractModel):
    policy_name: str = Field(alias="policyName", min_length=1)
    based_on_state_revision: int = Field(alias="basedOnStateRevision", ge=0)
    mode: Literal["fixed", "adapt"]
    requested_level: RequestedLevelValue = Field(alias="requestedLevel")
    resolved_level: GuidanceLevelValue = Field(alias="resolvedLevel")
    reason: str = Field(min_length=1)
    progression: Optional[Dict[str, Any]] = None


class GuidanceSet(ContractModel):
    set_id: str = Field(alias="setId", min_length=1)
    strategy_id: str = Field(alias="strategyId", min_length=1)
    guidance_topic_id: str = Field(alias="guidanceTopicId", min_length=1)
    state: GuidanceStateValue
    effective_level: GuidanceLevelValue = Field(alias="effectiveLevel")
    target_widget_ids: List[str] = Field(alias="targetWidgetIds")
    enabled_action_kinds: List[str] = Field(alias="enabledActionKinds")
    candidates: List[GuidanceCandidate]


class GuidanceResult(ContractModel):
    based_on_state_revision: int = Field(alias="basedOnStateRevision", ge=0)
    decision: Optional[PolicyDecision] = None
    sets: List[GuidanceSet] = Field(default_factory=list)


class FeedbackRequest(ContractModel):
    event_id: str = Field(alias="eventId", min_length=1)
    candidate_id: str = Field(alias="candidateId", min_length=1)
    based_on_state_revision: int = Field(alias="basedOnStateRevision", ge=0)
    response: Literal["accept", "reject", "snooze"]
    requested_level: RequestedLevelValue = Field(
        "adapt",
        alias="requestedLevel",
    )


class PlaygroundPreset(ContractModel):
    widget_id: str = Field(alias="widgetId", min_length=1)
    entrypoint: str = Field(min_length=1)
    documents: Dict[str, str]


class PlaygroundRunRequest(PlaygroundPreset):
    pass


class PlaygroundRunResult(ContractModel):
    widget_id: str = Field(alias="widgetId", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)
