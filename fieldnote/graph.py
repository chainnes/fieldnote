from __future__ import annotations

import contextvars
import json
from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from fieldnote.config import Settings
from fieldnote.extract import message_text
from fieldnote.llm import PROPOSE_SYSTEM, RESEARCH_SYSTEM, invoke_packet
from fieldnote.repair import run_with_repair
from fieldnote.tools import CURRENT_STORE, TOOLS
from fieldnote.validate import validate_drafts

CURRENT_SETTINGS: contextvars.ContextVar[Settings] = contextvars.ContextVar("fieldnote_settings")


class PlannerState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    project_id: str
    source_text: str
    tool_rounds: int
    raw_proposals: list
    proposals: list
    notes: list
    error: str
    model_notes: str
    off_schema: bool
    off_schema_error: str


def agent_node(state: PlannerState) -> dict:
    from fieldnote.llm import build_model

    model = build_model(CURRENT_SETTINGS.get()).bind_tools(TOOLS)
    reply = model.invoke(state["messages"])
    return {"messages": [reply]}


def tools_node(state: PlannerState) -> dict:
    from langgraph.prebuilt import ToolNode

    result = ToolNode(TOOLS).invoke(state)
    result["tool_rounds"] = state.get("tool_rounds", 0) + 1
    return result


def route(state: PlannerState) -> str:
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []
    if calls and state.get("tool_rounds", 0) < 4:
        return "tools"
    return "propose"


def _observation_block(messages) -> str:
    payloads = []
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        payloads.append({"tool": getattr(message, "name", ""), "content": message_text(message)})
    return json.dumps(payloads, ensure_ascii=False)


def propose_node(state: PlannerState) -> dict:
    settings = CURRENT_SETTINGS.get()
    human = (
        f"PROJECT_ID: {state['project_id']}\n"
        f"SOURCE_TEXT:\n{state['source_text']}\n"
        f"<<<FIELDNOTE_OBSERVATIONS>>>\n{_observation_block(state['messages'])}\n"
        f"<<<END_OBSERVATIONS>>>"
    )
    messages = [SystemMessage(content=PROPOSE_SYSTEM), HumanMessage(content=human)]
    repair_messages = messages + [
        HumanMessage(
            content=(
                "The previous output did not match the schema. "
                "Return one JSON object only, matching ReviewPacket, with no markdown."
            )
        )
    ]

    def parse():
        return invoke_packet(settings, messages)

    def repair():
        return invoke_packet(settings, repair_messages)

    packet, status, error = run_with_repair(parse, repair)
    if packet is None:
        return {
            "off_schema": True,
            "off_schema_error": error,
            "raw_proposals": [],
            "model_notes": "",
            "error": "",
        }
    notes = packet.model_notes or ""
    if status == "repaired":
        notes = f"{notes} Repaired once after an off-schema response.".strip()
    return {
        "raw_proposals": [item.model_dump() for item in packet.proposals],
        "model_notes": notes,
        "off_schema": False,
        "error": "",
    }


def validate_node(state: PlannerState) -> dict:
    notes = []
    if state.get("model_notes"):
        notes.append(state["model_notes"])
    if state.get("error"):
        return {"proposals": [], "notes": notes + [state["error"]]}
    store = CURRENT_STORE.get()
    from fieldnote.schemas import ModelProposal

    drafts = []
    for raw in state.get("raw_proposals") or []:
        try:
            drafts.append(ModelProposal.model_validate(raw))
        except Exception:
            notes.append("Dropped a proposal that did not match the schema.")
    kept, extra = validate_drafts(drafts, state.get("source_text") or "", store, state["project_id"])
    return {"proposals": kept, "notes": notes + extra}


def build_graph():
    builder = StateGraph(PlannerState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("propose", propose_node)
    builder.add_node("validate", validate_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", "propose": "propose"})
    builder.add_edge("tools", "agent")
    builder.add_edge("propose", "validate")
    builder.add_edge("validate", END)
    return builder.compile()


GRAPH = build_graph()


def preamble(project: dict, source: dict) -> str:
    return (
        f"PROJECT_ID: {project['project_id']}\n"
        f"PROJECT_NAME: {project['project_name']}\n"
        f"SOURCE_KIND: {source['kind']}\n"
        f"SOURCE_ID: {source['source_id']}\n"
        f"SOURCE_DATE: {source['when']}\n"
        f"SOURCE_TEXT:\n{source['text']}\n"
    )


def tool_trace(messages) -> list[dict]:
    trace = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            trace.append({"tool": call.get("name"), "args": call.get("args") or {}})
    return trace
