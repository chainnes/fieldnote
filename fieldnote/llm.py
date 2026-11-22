from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from fieldnote.config import Settings
from fieldnote.extract import (
    dependency_calls,
    observations_from_messages,
    packet_from_observations,
    project_id_from_messages,
    source_from_messages,
    tool_plan,
)
from fieldnote.schemas import ReviewPacket, coerce_packet

RESEARCH_SYSTEM = """You are Fieldnote's research step. You do not change the plan.
Call tools until you can ground the source:
- project_risks once for the project
- search_tasks once for each distinct work item, using the source's own wording as the query
- search_sources once for each work item, to retrieve evidence chunks from notes and email
- resolve_person once for each person named as an owner
- task_dependencies for a task only after search_tasks returned that id and the match is unambiguous
When you have what you need, reply with exactly READY and no tool calls.
Never invent a task id or a person id."""

PROPOSE_SYSTEM = """You draft proposed plan changes for a human reviewer. You never apply them.
Return only proposals that follow the schema.
Rules:
- evidence_quote must be copied verbatim from SOURCE_TEXT. If you cannot quote the line, omit the proposal.
- task_id must be copied from search_tasks results. If several tasks fit, or none do, set task_id to null and kind to conflict.
- kind new_task only when the source explicitly asks to create work and no single existing task is clearly the same work.
- kind status_update for blocked, done, in progress, or not started. kind date_change for a due date. kind owner_change when a person is assigned.
- If the source asks a question ("who owns") rather than deciding, use kind conflict.
- confidence is between 0 and 1. Set needs_human true when the link is fuzzy, the owner is tentative, or you are unsure.
- urgency_note is one sentence on why the item is urgent or not. The desk still computes the numeric score itself.
- Do not propose anything from signatures, greetings, or the project-risk summary. The risk summary is context only.
- At most 8 proposals."""


class MockPlanner(BaseChatModel):
    """Deterministic stand-in that still emits tool calls and a schema-checked packet.

    The graph, the tools, and the validator are the same ones a live model uses.
    """

    tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fieldnote-mock"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.model_copy(update={"tools": list(tools)})

    def with_structured_output(self, schema, **kwargs):
        def _call(messages):
            source = source_from_messages(messages)
            observations = observations_from_messages(messages)
            payload = packet_from_observations(source, observations)
            if schema is ReviewPacket or schema is None:
                return coerce_packet(payload)
            return schema.model_validate(payload)

        return RunnableLambda(_call)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        tool_messages = [message for message in messages if getattr(message, "type", None) == "tool"]
        if not tool_messages:
            project_id = project_id_from_messages(messages)
            source = source_from_messages(messages)
            planned = tool_plan(source, project_id)
            tool_calls = [
                {
                    "name": call["name"],
                    "args": call["args"],
                    "id": f"call_{index}_{call['name']}",
                    "type": "tool_call",
                }
                for index, call in enumerate(planned, start=1)
            ]
            message = AIMessage(content="", tool_calls=tool_calls)
            return ChatResult(generations=[ChatGeneration(message=message)])

        observations = observations_from_messages(messages)
        already = {getattr(message, "name", "") for message in tool_messages}
        if "task_dependencies" not in already:
            follow = dependency_calls(observations)
            if follow:
                tool_calls = [
                    {
                        "name": call["name"],
                        "args": call["args"],
                        "id": f"call_dep_{index}_{call['args']['task_id']}",
                        "type": "tool_call",
                    }
                    for index, call in enumerate(follow, start=1)
                ]
                message = AIMessage(content="", tool_calls=tool_calls)
                return ChatResult(generations=[ChatGeneration(message=message)])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="READY"))])


def build_model(settings: Settings):
    if settings.provider == "mock":
        return MockPlanner()
    if settings.provider in {"openai", "compatible"}:
        if settings.provider == "openai" and not settings.api_key:
            raise RuntimeError("LLM_API_KEY is not set.")
        if settings.provider == "compatible" and not settings.base_url:
            raise RuntimeError("LLM_BASE_URL is not set for the compatible provider.")
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": settings.model,
            "temperature": settings.temperature,
            "api_key": settings.api_key or "not-set",
            "timeout": 90,
            "max_retries": 1,
        }
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        return ChatOpenAI(**kwargs)
    if settings.provider == "anthropic":
        if not settings.api_key:
            raise RuntimeError("LLM_API_KEY is not set.")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.model,
            temperature=settings.temperature,
            api_key=settings.api_key,
            timeout=90,
            max_retries=1,
        )
    raise RuntimeError(
        f"Unknown LLM_PROVIDER {settings.provider!r}. Use mock, openai, anthropic, or compatible."
    )


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The model did not return JSON.")
    return json.loads(text[start : end + 1])


def invoke_packet(settings: Settings, messages) -> ReviewPacket:
    model = build_model(settings)
    if settings.provider == "compatible":
        return _invoke_json(model, messages)
    try:
        result = model.with_structured_output(ReviewPacket).invoke(messages)
    except Exception:
        if settings.provider == "mock":
            raise
        return _invoke_json(model, messages)
    return coerce_packet(result)


def _invoke_json(model, messages) -> ReviewPacket:
    from langchain_core.messages import HumanMessage

    schema = json.dumps(ReviewPacket.model_json_schema())
    raw = model.invoke(
        list(messages)
        + [
            HumanMessage(
                content=(
                    "Reply with one JSON object only, matching this schema. "
                    "Do not wrap it in markdown.\n"
                    + schema
                )
            )
        ]
    )
    content = raw.content if isinstance(raw.content, str) else json.dumps(raw.content)
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.I | re.M)
    return coerce_packet(_extract_json(content))
