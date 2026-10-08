"""Conversions between GuidanceWidgets data and Lotse objects."""

from collections import Counter, OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .contracts import (
    GuidanceAction,
    GuidanceCandidate,
    GuidancePresentation,
    GuidanceResult,
    GuidanceSet,
    GuidanceSummary,
    PolicyDecision,
    StateSnapshot,
)


# Maps concise YAML action names to GuidanceWidget action kinds.
ACTION_KINDS = {
    "tooltip": "common.tooltip",
    "highlight": "common.highlight",
    "focus-values": "common.focus-values",
    "propose-value": "common.propose-value",
    "revert": "history.revert",
}


def build_context(
    snapshot: StateSnapshot,
    revision: int,
    observed_at: Optional[str],
) -> Dict[str, Any]:
    """Create the context exposed to Lotse YAML callbacks."""

    widgets_by_id = {widget.widget_id: widget for widget in snapshot.widgets}
    interactions = []
    for item in snapshot.interaction_sequence:
        widget = widgets_by_id[item.widget_id]
        record = widget.data[item.record_index]
        interactions.append({
            "sequence": item.sequence,
            "widgetId": widget.widget_id,
            "widgetType": widget.widget_type,
            "value": record.value,
            "timestamp": record.timestamp,
            "source": record.source,
        })

    user_interactions = [
        item for item in interactions if item["source"] == "user"
    ]
    recent = user_interactions[-10:] # Get the last 10 user interactions for context.
    interaction_counts = Counter(
        item["widgetId"] for item in user_interactions
    )

    widget_contexts = {}
    for widget in sorted(snapshot.widgets, key=lambda item: item.widget_id):
        current = widget.data[-1] if widget.data else None
        changes = [record for record in widget.data if record.kind != "sample"]
        previous = changes[-2] if len(changes) > 1 else None
        last_change = changes[-1] if changes else None
        widget_contexts[widget.widget_id] = {
            "widgetId": widget.widget_id,
            "widgetType": widget.widget_type,
            "hasPreviousValue": previous is not None,
            "previousValue": previous.value if previous else None,
            "presentValue": current.value if current else None,
            "changed": (
                previous is not None
                and current is not None
                and previous.value != current.value
            ),
            "lastChangedAt": last_change.timestamp if last_change else None,
            "lastChangedBy": last_change.source if last_change else None,
            "userInteractionCount": interaction_counts.get(widget.widget_id, 0),
            "historyValues": [record.value for record in widget.data],
            "historyRecords": [
                {
                    "value": record.value,
                    "source": record.source,
                    "kind": record.kind,
                }
                for record in widget.data
            ],
        }

    switch_count = sum(
        left["widgetId"] != right["widgetId"]
        for left, right in zip(recent, recent[1:])
    )
    immediate_return_count = sum(
        recent[index]["widgetId"] == recent[index - 2]["widgetId"]
        and recent[index]["widgetId"] != recent[index - 1]["widgetId"]
        for index in range(2, len(recent))
    )
    current_run_length = 0
    if user_interactions:
        current_widget_id = user_interactions[-1]["widgetId"]
        for item in reversed(user_interactions):
            if item["widgetId"] != current_widget_id:
                break
            current_run_length += 1

    return {
        "stateRevision": revision,
        "stateObservedAt": observed_at,
        "widgets": widget_contexts,
        "core": {
            "lastUserInteraction": (
                user_interactions[-1] if user_interactions else None
            ),
            "recentUserInteractions": recent,
            "activity": {
                "userInteractionCount": len(user_interactions),
            },
            "sequence": {
                "recentWindowSize": 10,
                "switchCount": switch_count,
                "immediateReturnCount": immediate_return_count,
                "currentSameWidgetRunLength": current_run_length,
            },
        },
        "app": dict(snapshot.application_context),
    }


def build_delta(
    previous: Optional[Dict[str, Any]],
    current: Dict[str, Any],
) -> Dict[str, Any]:
    previous_widgets = previous.get("widgets", {}) if previous else {}
    changed_widgets = {}
    for widget_id, widget in current["widgets"].items():
        old_value = previous_widgets.get(widget_id, {}).get("presentValue")
        new_value = widget.get("presentValue")
        if not previous or old_value != new_value:
            changed_widgets[widget_id] = {
                "previousValue": old_value,
                "presentValue": new_value,
            }
    return {
        "stateRevision": current["stateRevision"],
        "widgets": changed_widgets,
        "app": current["app"],
    }


def apply_context(engine: Any, context: Dict[str, Any]) -> None:
    engine.current_state.__dict__.update(context)


def suggestion_to_candidate(suggestion_model: Any, revision: int) -> GuidanceCandidate:
    return _suggestion_to_candidate(
        suggestion_model,
        revision,
        suggestion_model.suggestion.event.value,
        suggestion_model.suggestion.id,
    )


def suggestion_to_candidates(
    suggestion_model: Any,
    revision: int,
) -> List[GuidanceCandidate]:
    suggestion = suggestion_model.suggestion
    content = suggestion.event.value
    if not isinstance(content, dict) or not isinstance(
        content.get("candidates"),
        list,
    ):
        return [suggestion_to_candidate(suggestion_model, revision)]

    candidates = []
    for index, candidate_content in enumerate(content["candidates"]):
        if not isinstance(candidate_content, dict):
            raise ValueError("Expanded guidance candidates must be objects")
        candidates.append(_suggestion_to_candidate(
            suggestion_model,
            revision,
            candidate_content,
            "{}:{}".format(suggestion.id, index),
        ))
    return candidates


def _suggestion_to_candidate(
    suggestion_model: Any,
    revision: int,
    content: Any,
    candidate_id: str,
) -> GuidanceCandidate:
    suggestion = suggestion_model.suggestion
    action = suggestion_model.action
    action_metadata = _mapping(getattr(action, "metadata", None))
    strategy_metadata = _mapping(
        getattr(getattr(action, "strategy", None), "metadata", None)
    )
    guidance = _guidance_metadata(action_metadata, strategy_metadata)
    actions, evidence = _actions_and_evidence(
        content,
        suggestion,
        action_metadata,
        guidance,
    )
    candidate_metadata = _mapping(content)
    summary = GuidanceSummary(
        title=candidate_metadata.get("title", suggestion.title) or None,
        message=(
            candidate_metadata.get("message", suggestion.description)
            or None
        ),
    )
    presentation = guidance.get("presentation")

    configured_level = guidance.get("level")
    level = configured_level if configured_level in {0, 1, 2, 3} else None

    return GuidanceCandidate(
        candidateId=candidate_id,
        strategyId=suggestion.strategy,
        guidanceTopicId=(
            guidance.get("topic")
            or guidance.get("topic_id")
            or suggestion.strategy
        ),
        basedOnStateRevision=revision,
        state=guidance.get("state", "present"),
        priority=candidate_metadata.get(
            "priority",
            action_metadata.get(
                "priority",
                strategy_metadata.get("priority", 0),
            ),
        ),
        level=level,
        maxLevel=guidance.get("max_level", guidance.get("maxLevel")),
        summary=summary,
        presentation=(
            GuidancePresentation(**presentation)
            if isinstance(presentation, dict)
            else None
        ),
        actions=actions,
        sourceProblem=guidance.get(
            "source_problem",
            guidance.get("sourceProblem"),
        ),
        evidence=evidence,
        optionsCount=guidance.get(
            "options_count",
            guidance.get("optionsCount"),
        ),
    )


def build_result(
    suggestions: Iterable[Any],
    revision: int,
    requested_level: Optional[Union[int, str]],
    top_n: int = 3,
) -> GuidanceResult:
    candidates = [
        candidate
        for suggestion in suggestions
        for candidate in suggestion_to_candidates(suggestion, revision)
    ]
    return build_result_from_candidates(
        candidates,
        revision,
        requested_level,
        top_n,
    )


def build_result_from_candidates(
    candidates: Iterable[GuidanceCandidate],
    revision: int,
    requested_level: Optional[Union[int, str]],
    top_n: int = 3,
) -> GuidanceResult:
    candidates = list(candidates)
    candidates.sort(key=lambda candidate: candidate.priority, reverse=True)
    resolved_level, mode, requested, reason = _resolve_level(
        candidates,
        requested_level,
    )
    sets = (
        _build_sets(candidates, resolved_level, top_n, mode == "fixed")
        if resolved_level > 0
        else []
    )
    return GuidanceResult(
        basedOnStateRevision=revision,
        decision=PolicyDecision(
            policyName="lotse",
            basedOnStateRevision=revision,
            mode=mode,
            requestedLevel=requested,
            resolvedLevel=resolved_level,
            reason=reason,
        ),
        sets=sets,
    )


def _guidance_metadata(
    action_metadata: Dict[str, Any],
    strategy_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    strategy_guidance = _mapping(strategy_metadata.get("guidance"))
    action_guidance = _mapping(action_metadata.get("guidance"))
    return dict(strategy_guidance, **action_guidance)


def _actions_and_evidence(
    content: Any,
    suggestion: Any,
    metadata: Dict[str, Any],
    guidance: Dict[str, Any],
) -> Tuple[List[GuidanceAction], Dict[str, Any]]:
    if isinstance(content, dict) and isinstance(content.get("actions"), list):
        actions = [
            _normalize_action(item, guidance)
            for item in content["actions"]
        ]
        evidence = _mapping(content.get("evidence"))
        return actions, evidence

    target_widget_id = (
        guidance.get("widget")
        or guidance.get("target_widget_id")
        or guidance.get("targetWidgetId")
        or metadata.get("target_widget_id")
        or suggestion.event.action_id
    )
    configured_kind = guidance.get("action") or suggestion.event.action_id
    kind = ACTION_KINDS.get(configured_kind, configured_kind)
    values = {
        "kind": kind,
        "targetWidgetId": target_widget_id,
    }

    if kind == "common.tooltip":
        values["message"] = (
            content if isinstance(content, str) else suggestion.description
        )
    elif kind == "common.highlight":
        if isinstance(content, dict) and "kind" in content:
            values["target"] = content
        else:
            target_kind = "options" if isinstance(content, list) else "value"
            values["target"] = {"kind": target_kind, "value": content}
    elif kind == "common.focus-values":
        values["values"] = content if isinstance(content, list) else [content]
    elif kind == "common.propose-value":
        values["value"] = content
    elif kind == "history.revert":
        values.update({"snapshot": "previous", "value": content})
    else:
        values["value"] = content

    evidence = (
        _mapping(content.get("evidence"))
        if isinstance(content, dict)
        else {}
    )
    return [GuidanceAction(**values)], evidence


def _normalize_action(
    action: Any,
    guidance: Dict[str, Any],
) -> GuidanceAction:
    if not isinstance(action, dict):
        raise ValueError("Guidance actions must be objects")
    values = dict(action)
    values["kind"] = ACTION_KINDS.get(values.get("kind"), values.get("kind"))
    if "targetWidgetId" not in values:
        values["targetWidgetId"] = (
            guidance.get("widget")
            or guidance.get("target_widget_id")
            or guidance.get("targetWidgetId")
        )
    return GuidanceAction(**values)


def _resolve_level(
    candidates: List[GuidanceCandidate],
    requested_level: Optional[Union[int, str]],
) -> Tuple[int, str, Union[int, str], str]:
    requested = "adapt" if requested_level is None else requested_level
    if not candidates:
        mode = "adapt" if requested == "adapt" else "fixed"
        return 0, mode, requested, "no-applicable-suggestion"
    if requested != "adapt":
        level = int(requested)
        reason = "guidance-disabled" if level == 0 else "requested-level"
        return level, "fixed", level, reason

    level = candidates[0].level
    if level is None:
        level = 1
    if candidates[0].max_level is not None:
        level = min(level, candidates[0].max_level)
    return level, "adapt", "adapt", "highest-priority-suggestion"


def _build_sets(
    candidates: List[GuidanceCandidate],
    level: int,
    top_n: int,
    fixed_level: bool,
) -> List[GuidanceSet]:
    groups = OrderedDict()
    for candidate in candidates:
        key = (
            candidate.strategy_id,
            candidate.guidance_topic_id,
            candidate.state,
        )
        groups.setdefault(key, []).append(candidate)

    sets = []
    for (strategy_id, topic_id, state), group in groups.items():
        configured_level = next(
            (candidate.level for candidate in group if candidate.level is not None),
            None,
        )
        effective_level = (
            level
            if fixed_level or configured_level is None
            else configured_level
        )
        configured_max_level = next(
            (
                candidate.max_level
                for candidate in group
                if candidate.max_level is not None
            ),
            None,
        )
        if configured_max_level is not None:
            effective_level = min(effective_level, configured_max_level)
        if effective_level == 0:
            continue
        if effective_level == 1:
            selected = group
        elif effective_level == 2:
            selected = group[:top_n]
        else:
            selected = group[:1]
        if not selected:
            continue

        target_widget_ids = sorted({
            action.target_widget_id
            for candidate in selected
            for action in candidate.actions
        })
        sets.append(GuidanceSet(
            setId="{}:{}:{}".format(strategy_id, topic_id, state),
            strategyId=strategy_id,
            guidanceTopicId=topic_id,
            state=state,
            effectiveLevel=effective_level,
            targetWidgetIds=target_widget_ids,
            enabledActionKinds=_enabled_action_kinds(selected),
            candidates=selected,
        ))
    return sets


def _enabled_action_kinds(
    candidates: List[GuidanceCandidate],
) -> List[str]:
    ordered = []
    for candidate in candidates:
        for action in candidate.actions:
            if action.kind in ordered:
                continue
            ordered.append(action.kind)
    return ordered


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}
