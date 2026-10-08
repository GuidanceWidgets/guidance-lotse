"""GuidanceWidgets transport adapter for native Lotse engines."""

import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Callable, Dict, Optional, Union

import yaml

from .contracts import (
    FeedbackRequest,
    GuidanceCandidate,
    GuidanceResult,
    SessionState,
    StateSnapshot,
)
from .mapper import (
    apply_context,
    build_context,
    build_delta,
    build_result,
    build_result_from_candidates,
    suggestion_to_candidates,
)


class FeedbackConflict(ValueError):
    pass


class PlaygroundSessionExpired(ValueError):
    pass


class PlaygroundConfigError(ValueError):
    def __init__(
        self,
        message: str,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
    ):
        super().__init__(message)
        self.file_path = file_path
        self.line = line
        self.column = column

    def detail(self):
        return {
            "message": str(self),
            "file": self.file_path,
            "line": self.line,
            "column": self.column,
        }


@dataclass
class PresentedGuidance:
    suggestion: Any
    candidate: GuidanceCandidate


@dataclass
class GuidanceSession:
    engine: Any
    state: Optional[SessionState] = None
    context: Optional[Dict[str, Any]] = None
    presented: Dict[str, PresentedGuidance] = field(default_factory=dict)
    feedback_events: Dict[str, FeedbackRequest] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)


class GuidanceWidgetBackend:
    """Expose native Lotse recommendation state through widget contracts.

    Each session owns one Lotse engine. State updates only replace the Lotse
    context; the widget client's two-second guidance poll drives Lotse's
    action evaluation, matching the native demo's guidance loop. Feedback
    state remains inside the YAML-backed action instances.
    """

    def __init__(
        self,
        strategy_path: Union[str, Path],
        state_path: Union[str, Path],
        engine_factory: Optional[Callable[[], Any]] = None,
        top_n: int = 3,
    ):
        if top_n < 1:
            raise ValueError("top_n must be positive")
        self.strategy_path = Path(strategy_path)
        self.state_path = Path(state_path)
        self.top_n = top_n
        self._engine_factory = engine_factory or self._create_engine
        self._sessions: Dict[str, GuidanceSession] = {}
        self._sessions_lock = RLock()
        self._playground_sessions = []

    @classmethod
    def from_environment(cls):
        strategy_path = os.getenv("LOTSE_STRATEGY_PATH")
        state_path = os.getenv("LOTSE_STATE_PATH")
        if not strategy_path or not state_path:
            raise RuntimeError(
                "LOTSE_STRATEGY_PATH and LOTSE_STATE_PATH are required"
            )
        return cls(
            strategy_path=strategy_path,
            state_path=state_path,
            top_n=int(os.getenv("LOTSE_GUIDANCE_TOP_N", "3")),
        )

    def replace_state(
        self,
        session_id: str,
        snapshot: StateSnapshot,
    ) -> SessionState:
        session = self._session(session_id)
        with session.lock:
            revision = session.state.revision + 1 if session.state else 1
            observed_at = datetime.now(timezone.utc).isoformat()
            state = SessionState(
                session_id=session_id,
                revision=revision,
                observedAt=observed_at,
                widgets=sorted(
                    snapshot.widgets,
                    key=lambda widget: widget.widget_id,
                ),
                interactionSequence=snapshot.interaction_sequence,
                applicationContext=snapshot.application_context,
            )
            context = build_context(snapshot, revision, observed_at)

            session.engine.last_delta = build_delta(session.context, context)
            apply_context(session.engine, context)
            session.state = state
            session.context = context
            # A candidate must be presented again for the new transport
            # revision, while the underlying Lotse suggestion stays active.
            session.presented.clear()
            return state

    def get_guidance(
        self,
        session_id: str,
        requested_level: Optional[Union[int, str]] = None,
    ) -> GuidanceResult:
        session = self._session(session_id)
        with session.lock:
            if session.state is None:
                return build_result([], 0, requested_level, self.top_n)
            self._evaluate(session)
            return self._current_result(session, requested_level)

    def submit_feedback(
        self,
        session_id: str,
        feedback: FeedbackRequest,
    ) -> GuidanceResult:
        session = self._session(session_id)
        with session.lock:
            existing = session.feedback_events.get(feedback.event_id)
            if existing is not None:
                if existing != feedback:
                    raise FeedbackConflict(
                        'Feedback event "{}" already exists'.format(
                            feedback.event_id
                        )
                    )
                return self._current_result(
                    session,
                    feedback.requested_level,
                )

            if session.state is None:
                raise FeedbackConflict("The session has no state")
            if feedback.based_on_state_revision != session.state.revision:
                raise FeedbackConflict(
                    "Feedback is based on a stale state revision"
                )

            presented = session.presented.get(feedback.candidate_id)
            if presented is None:
                raise FeedbackConflict(
                    "Feedback candidate is not in the current guidance result"
                )
            suggestion = presented.suggestion

            callback = getattr(
                suggestion.action,
                feedback.response,
                None,
            )
            if callable(callback):
                callback(
                    suggestion,
                    session.engine.current_state,
                    session.engine.last_delta,
                )
            else:
                raise FeedbackConflict(
                    'Action does not implement "{}"'.format(
                        feedback.response
                    )
                )

            session.feedback_events[feedback.event_id] = feedback
            session.engine.suggestions = [
                item
                for item in session.engine.suggestions
                if item.suggestion.id != suggestion.suggestion.id
            ]
            return self._current_result(session, feedback.requested_level)

    def get_playground_preset(self, widget_id: str):
        for strategy_file in sorted(self.strategy_path.glob("*.yaml")):
            if strategy_file.name == "meta.yaml":
                continue
            strategy_text = strategy_file.read_text(encoding="utf-8")
            strategy = self._parse_yaml(strategy_file.name, strategy_text)
            guidance = self._guidance_metadata(strategy, strategy_file.name)
            if guidance.get("widget") != widget_id:
                continue

            action_path = self._action_path(strategy, strategy_file.name)
            action_file = self.strategy_path.joinpath(*PurePosixPath(action_path).parts)
            if not action_file.is_file():
                raise PlaygroundConfigError(
                    "Referenced action file does not exist",
                    action_path,
                )
            return {
                "widgetId": widget_id,
                "entrypoint": strategy_file.name,
                "documents": {
                    strategy_file.name: strategy_text,
                    action_path: action_file.read_text(encoding="utf-8"),
                },
            }

        raise PlaygroundConfigError(
            'No strategy is configured for widget "{}"'.format(widget_id)
        )

    def create_playground_session(
        self,
        widget_id: str,
        entrypoint: str,
        documents: Dict[str, str],
    ) -> str:
        normalized_entrypoint = self._document_path(entrypoint)
        if (
            len(PurePosixPath(normalized_entrypoint).parts) != 1
            or not normalized_entrypoint.endswith(".yaml")
            or normalized_entrypoint == "meta.yaml"
        ):
            raise PlaygroundConfigError(
                "The strategy entrypoint must be a top-level .yaml file other than meta.yaml",
                entrypoint,
            )
        if not documents or len(documents) > 8:
            raise PlaygroundConfigError("A run must contain 1 to 8 YAML files")
        if sum(len(value) for value in documents.values()) > 200_000:
            raise PlaygroundConfigError("The YAML configuration is too large")

        normalized = {}
        parsed = {}
        for file_path, content in documents.items():
            path = self._document_path(file_path)
            if path in normalized:
                raise PlaygroundConfigError("Duplicate YAML file", path)
            normalized[path] = content
            parsed[path] = self._parse_yaml(path, content)

        if normalized_entrypoint not in normalized:
            raise PlaygroundConfigError(
                "The strategy entrypoint is missing",
                normalized_entrypoint,
            )
        strategy = parsed[normalized_entrypoint]
        guidance = self._guidance_metadata(strategy, normalized_entrypoint)
        if guidance.get("widget") != widget_id:
            raise PlaygroundConfigError(
                'metadata.guidance.widget must be "{}"'.format(widget_id),
                normalized_entrypoint,
            )
        if guidance.get("state") not in ("past", "present", "problem", "future"):
            raise PlaygroundConfigError(
                "metadata.guidance.state must be past, present, problem, or future",
                normalized_entrypoint,
            )
        if type(guidance.get("level")) is not int or guidance["level"] not in {0, 1, 2, 3}:
            raise PlaygroundConfigError(
                "metadata.guidance.level must be 0, 1, 2, or 3",
                normalized_entrypoint,
            )

        action_path = self._action_path(strategy, normalized_entrypoint)
        if len(PurePosixPath(action_path).parts) < 2:
            raise PlaygroundConfigError(
                "Keep the action in a subfolder such as actions/ so Lotse loads only one strategy",
                normalized_entrypoint,
            )
        if action_path not in normalized:
            raise PlaygroundConfigError(
                "The referenced action YAML is missing from this run",
                action_path,
            )
        extra_files = set(normalized) - {normalized_entrypoint, action_path}
        if extra_files:
            raise PlaygroundConfigError(
                "This playground currently supports one strategy and one action",
                sorted(extra_files)[0],
            )

        for file_path, content in normalized.items():
            self._validate_python(file_path, content, parsed[file_path])

        try:
            with tempfile.TemporaryDirectory(prefix="lotse-playground-") as folder:
                root = Path(folder)
                for file_path, content in normalized.items():
                    destination = root.joinpath(*PurePosixPath(file_path).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8")
                engine = self._create_engine_from_paths(root, self.state_path)
        except PlaygroundConfigError:
            raise
        except Exception as error:
            raise PlaygroundConfigError(
                "Lotse could not load this configuration: {}".format(error),
                normalized_entrypoint,
            ) from error

        session_id = "playground-{}".format(uuid.uuid4())
        with self._sessions_lock:
            self._sessions[session_id] = GuidanceSession(engine=engine)
            self._playground_sessions.append(session_id)
            while len(self._playground_sessions) > 50:
                expired = self._playground_sessions.pop(0)
                self._sessions.pop(expired, None)
        return session_id

    def _session(self, session_id: str) -> GuidanceSession:
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if session is None:
                if session_id.startswith("playground-"):
                    raise PlaygroundSessionExpired("This Live Editor session expired. Click Run again.")
                session = GuidanceSession(engine=self._engine_factory())
                self._sessions[session_id] = session
            return session

    def _create_engine(self):
        return self._create_engine_from_paths(
            self.strategy_path,
            self.state_path,
        )

    @staticmethod
    def _create_engine_from_paths(strategy_path: Path, state_path: Path):
        import lotse
        from lotse import action

        # gsrickled 1.0 still imports Lotse's former package name.
        sys.modules.setdefault("guidance_strategies", lotse)
        sys.modules.setdefault("guidance_strategies.action", action)

        from lotse.app.guidance_engine.lotse_engine import LotseEngine

        engine = LotseEngine(
            str(strategy_path),
            str(state_path),
            "meta.yaml",
        )
        # GuidanceAPI.setup_engine() starts with every loaded strategy active
        # and materializes its YAML actions once. Do the same here; later
        # guidance requests only run Lotse's native action evaluation.
        engine.applicable_strategies = engine.strategies
        engine.generate_conditional_actions()
        return engine

    @staticmethod
    def _parse_yaml(file_path: str, content: str):
        try:
            value = yaml.safe_load(content)
        except yaml.MarkedYAMLError as error:
            mark = getattr(error, "problem_mark", None)
            raise PlaygroundConfigError(
                getattr(error, "problem", None) or "Invalid YAML",
                file_path,
                mark.line + 1 if mark else None,
                mark.column + 1 if mark else None,
            ) from error
        if not isinstance(value, dict):
            raise PlaygroundConfigError(
                "The YAML document must contain an object",
                file_path,
            )
        return value

    @staticmethod
    def _document_path(value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if (
            not value
            or ":" in value
            or "\0" in value
            or path.is_absolute()
            or ".." in path.parts
            or path.suffix not in {".yaml", ".yml"}
        ):
            raise PlaygroundConfigError("Invalid YAML file path", value)
        return path.as_posix()

    @classmethod
    def _action_path(cls, strategy: Dict[str, Any], strategy_file: str) -> str:
        action = strategy.get("action")
        if not isinstance(action, dict):
            raise PlaygroundConfigError("action must contain file_path", strategy_file)
        file_path = action.get("file_path")
        if not isinstance(file_path, str):
            raise PlaygroundConfigError(
                "action.file_path is required",
                strategy_file,
            )
        return cls._document_path(file_path)

    @staticmethod
    def _guidance_metadata(strategy, file_path):
        metadata = strategy.get("metadata")
        guidance = metadata.get("guidance") if isinstance(metadata, dict) else None
        if not isinstance(guidance, dict):
            raise PlaygroundConfigError("metadata.guidance must be an object", file_path)
        return guidance

    @staticmethod
    def _validate_python(file_path, content, parsed):
        """Locate syntax errors in the top-level Lotse callback bodies."""
        nodes = dict((key.value, value) for key, value in yaml.compose(content).value)
        for name, function in parsed.items():
            if not isinstance(function, dict) or not (
                function.get("type") == "function" or "args" in function
            ):
                continue
            body = function.get("load")
            if not isinstance(body, str):
                continue  # Let Lotse validate the remaining native grammar.
            function_node = nodes.get(str(name))
            if function_node is None:
                continue
            load_node = next(
                value for key, value in function_node.value if key.value == "load"
            )
            arguments = function.get("args")
            parameters = ["self"] + (
                [str(arg) for arg in arguments]
                if isinstance(arguments, (list, dict)) else []
            )
            try:
                # Rickle compiles a method with two extra spaces of indentation.
                source = "def callback({}):\n  {}".format(
                    ",".join(parameters), body.replace("\n", "\n  "),
                )
                compile(source, file_path, "exec")
            except SyntaxError as error:
                is_block = load_node.style in {"|", ">"}
                line = load_node.start_mark.line + (error.lineno if is_block else 1)
                raise PlaygroundConfigError(
                    "{}: {}".format(name, error.msg), file_path, line,
                ) from error

    @staticmethod
    def _evaluate(session: GuidanceSession) -> None:
        engine = session.engine
        retracted = engine.suggestions_to_retract()
        retracted_ids = {item.suggestion.id for item in retracted}
        for suggestion in retracted:
            suggestion.action.retract(
                engine.current_state,
                engine.last_delta,
                suggestion,
            )
        if retracted_ids:
            engine.suggestions = [
                item
                for item in engine.suggestions
                if item.suggestion.id not in retracted_ids
            ]

        engine.generate_suggestions()

    def _current_result(
        self,
        session: GuidanceSession,
        requested_level: Optional[Union[int, str]],
    ) -> GuidanceResult:
        revision = session.state.revision if session.state else 0
        available = {}
        for suggestion in session.engine.suggestions:
            for candidate in suggestion_to_candidates(suggestion, revision):
                presented = PresentedGuidance(
                    suggestion=suggestion,
                    candidate=candidate,
                )
                available[candidate.candidate_id] = presented

        result = build_result_from_candidates(
            (item.candidate for item in available.values()),
            revision,
            requested_level,
            self.top_n,
        )
        selected_ids = {
            candidate.candidate_id
            for guidance_set in result.sets
            for candidate in guidance_set.candidates
        }
        session.presented = {
            candidate_id: presented
            for candidate_id, presented in available.items()
            if candidate_id in selected_ids
        }
        return result
