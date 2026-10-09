"""Runtime-configurable routing policy: the dashboard's routing knobs.

Operators pin a task type to specific endpoints ("all embeddings go to
the-mini"), map model patterns to a home endpoint, and choose the
load-balancing strategy — from the dashboard, no yaml edit or restart.
gateway.yaml supplies the startup default; a value saved here overrides
it and survives restarts (same pattern as the PII scrub config).
"""

from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field, model_validator

from gateway.config import ModelDefault, ResolutionConfig, TaskEndpointPolicy
from gateway.models.common import TaskType

SETTING_KEY = "routing_config"


class TaskPin(BaseModel):
    """Which endpoints may serve a task (empty lists = no restriction)."""

    task: TaskType
    allowed_endpoints: list[str] = Field(default_factory=list, max_length=50)
    denied_endpoints: list[str] = Field(default_factory=list, max_length=50)


class ModelHome(BaseModel):
    """Preferred endpoint for a model pattern (glob)."""

    model: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=64)


class RoutingUpdate(BaseModel):
    """The PUT /api/routing/config payload."""

    strategy: Literal["priority", "least_loaded"] = "priority"
    task_endpoints: list[TaskPin] = Field(default_factory=list, max_length=20)
    model_defaults: list[ModelHome] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _one_policy_per_task(self) -> "RoutingUpdate":
        seen: set[TaskType] = set()
        for pin in self.task_endpoints:
            if pin.task in seen:
                raise ValueError(f"two policies for task '{pin.task.value}'")
            seen.add(pin.task)
        return self

    def referenced_endpoints(self) -> set[str]:
        names: set[str] = set()
        for pin in self.task_endpoints:
            names.update(pin.allowed_endpoints)
            names.update(pin.denied_endpoints)
        names.update(m.endpoint for m in self.model_defaults)
        return names


class RoutingState(BaseModel):
    """Provenance of the policy in effect, for the dashboard view."""

    source: str = "config"  # "config" (gateway.yaml / defaults) or "dashboard"
    updated_at: Any = None
    updated_by: str | None = None


def apply_routing(app: FastAPI, update: RoutingUpdate, state: RoutingState) -> None:
    """Make `update` the routing policy in effect, immediately.

    Replaces resolution strategy and model defaults (preserving the
    yaml-only endpoint_priority and ambiguous_behavior), swaps the task
    policies on the live enforcer, and updates config.task_endpoints so
    a later-created enforcer bridges the same policy.
    """
    config = app.state.config
    base = config.resolution
    config.resolution = ResolutionConfig(
        model_defaults=[
            ModelDefault(model=m.model, endpoint=m.endpoint) for m in update.model_defaults
        ],
        endpoint_priority=base.endpoint_priority,
        ambiguous_behavior=base.ambiguous_behavior,
        strategy=update.strategy,
    )
    config.task_endpoints = [
        TaskEndpointPolicy(
            task=pin.task,
            allowed_endpoints=pin.allowed_endpoints,
            denied_endpoints=pin.denied_endpoints,
        )
        for pin in update.task_endpoints
    ]

    enforcer = getattr(app.state, "enforcer", None)
    if enforcer is not None:
        from gateway.policy.enforcer import TaskProviderPolicy

        enforcer.set_task_policies(
            [
                TaskProviderPolicy(
                    task=pin.task,
                    allowed_providers=set(pin.allowed_endpoints),
                    denied_providers=set(pin.denied_endpoints),
                )
                for pin in update.task_endpoints
            ]
        )

    app.state.routing_state = state


def routing_from_config(app: FastAPI) -> RoutingUpdate:
    """The policy currently in effect, read back from app state."""
    config = app.state.config
    return RoutingUpdate(
        strategy=config.resolution.strategy,
        task_endpoints=[
            TaskPin(
                task=t.task,
                allowed_endpoints=list(t.allowed_endpoints),
                denied_endpoints=list(t.denied_endpoints),
            )
            for t in config.task_endpoints
        ],
        model_defaults=[
            ModelHome(model=m.model, endpoint=m.endpoint) for m in config.resolution.model_defaults
        ],
    )


async def load_saved_routing(app: FastAPI) -> None:
    """Apply a dashboard-saved routing policy at startup, if one exists."""
    try:
        saved = await app.state.runtime_settings.get(SETTING_KEY)
        if saved is None:
            return
        update = RoutingUpdate.model_validate(saved["value"])
        apply_routing(
            app,
            update,
            RoutingState(
                source="dashboard",
                updated_at=saved["updated_at"],
                updated_by=saved["updated_by"],
            ),
        )
        from gateway.observability import get_logger

        get_logger(__name__).info(
            "Routing policy loaded from dashboard setting",
            strategy=update.strategy,
            task_pins={p.task.value: p.allowed_endpoints for p in update.task_endpoints},
            updated_by=saved["updated_by"],
        )
    except Exception:
        from gateway.observability import get_logger

        get_logger(__name__).exception(
            "Saved routing policy unreadable; using gateway.yaml defaults"
        )
