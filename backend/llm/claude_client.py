"""
Anthropic Claude API client for PairProtocol.
All LLM calls go through this module.
Uses tool use (structured outputs) instead of raw JSON parsing for reliability.
"""

from __future__ import annotations
import os
import json
import logging
from typing import Any, Callable, Optional
from pathlib import Path

import anthropic

from backend.graph.client import GraphClient
from backend.knowledge.store import KnowledgeStore
from backend.session.models import (
    AnnotationStatus,
    CodeCoord,
    IntentAnnotation,
    Task,
    TaskPriority,
)
from backend.todo.store import TaskStore
from backend.llm import prompt_templates
from backend.llm.prompt_templates import build_system_prompt

log = logging.getLogger(__name__)

# Model identifiers
MODEL_PLANNING = "claude-opus-4-6"
MODEL_EXECUTION = "claude-sonnet-4-6"
_MAX_TOOL_ROUNDS = 12

# Tool schemas — Claude returns validated JSON matching these schemas
_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "targetFile": {"type": "string"},
                    "targetFunction": {"type": "string"},
                    "priority": {
                        "anyOf": [
                            {"type": "integer"},
                            {"type": "string", "enum": ["P0", "P1", "P2", "P3", "spike"]},
                        ]
                    },
                },
                "required": ["title", "description", "targetFile"],
            },
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Clarifying questions before the plan can be finalised, if any.",
        },
    },
    "required": ["tasks"],
}

_ANNOTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "detail": {"type": "string"},
        "willCreate": {"type": "array", "items": {"type": "string"}},
        "willModify": {"type": "array", "items": {"type": "string"}},
        "sideEffectWarnings": {"type": "array", "items": {"type": "string"}},
        "assumptionsMade": {"type": "array", "items": {"type": "string"}},
        "questionsBeforeProceeding": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "detail", "willCreate", "willModify"],
}

_CODE_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "targetFile": {"type": "string"},
                    "oldContent": {"type": "string"},
                    "newContent": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["targetFile", "newContent"],
            },
        },
        "stoppedReason": {
            "type": "string",
            "description": "If execution could not proceed as planned, explain why.",
        },
    },
    "required": ["edits"],
}

_QUESTION_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "planImpact": {
            "type": "string",
            "description": "How this answer affects the current plan, or null if it doesn't.",
        },
    },
    "required": ["text"],
}


def _tool(name: str, description: str, schema: dict) -> dict:
    return {"name": name, "description": description, "input_schema": schema}


def _coerce_priority(raw_priority: Any) -> TaskPriority:
    if isinstance(raw_priority, str):
        try:
            return TaskPriority(raw_priority)
        except ValueError:
            return TaskPriority.P2
    if isinstance(raw_priority, int):
        return {
            0: TaskPriority.P0,
            1: TaskPriority.P1,
            2: TaskPriority.P2,
            3: TaskPriority.P3,
        }.get(raw_priority, TaskPriority.P2)
    return TaskPriority.P2


class ClaudeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        graph: Optional[GraphClient] = None,
        knowledge_store: Optional[KnowledgeStore] = None,
        task_store_factory: Optional[Callable[[str], TaskStore]] = None,
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY or pass api_key= to ClaudeClient."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._graph = graph
        self._knowledge_store = knowledge_store
        self._task_store_factory = task_store_factory or (lambda workspace_path: TaskStore(workspace_path))
        self._task_stores: dict[str, TaskStore] = {}

    def generate_plan(
        self,
        goal: str,
        index_summary: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> tuple[list[Task], list[str]]:
        """
        Generate an ordered task list from a goal statement and index summary.
        Returns (tasks, clarifying_questions).
        """
        user_content = f"CODEBASE INDEX SUMMARY:\n{index_summary}\n\nGOAL: {goal}"

        response = self._run_structured_tool_turn(
            model=MODEL_PLANNING,
            max_tokens=4096,
            temperature=0.3,
            system=build_system_prompt("PLANNING", persona),
            user_content=user_content,
            final_tool_name="submit_plan",
            final_tool_description="Submit the implementation plan.",
            final_tool_schema=_TASK_SCHEMA,
            workspace_path=workspace_path,
        )

        tool_input = self._extract_tool_input(response, "submit_plan")
        raw_tasks = tool_input.get("tasks", [])
        questions = tool_input.get("questions", [])

        tasks = []
        for i, raw in enumerate(raw_tasks):
            task = Task(
                title=raw.get("title", ""),
                description=raw.get("description", ""),
                target_coord=CodeCoord(
                    file=raw.get("targetFile", ""),
                    method=raw.get("targetFunction"),
                ),
                priority=_coerce_priority(raw.get("priority", i)),
            )
            tasks.append(task)

        return tasks, questions

    def generate_annotation(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        """
        Generate an intent annotation for a task before any code is written.
        """
        user_content = f"{context}\n\nTASK:\nTitle: {task.title}\nDescription: {task.description}"

        response = self._run_structured_tool_turn(
            model=MODEL_EXECUTION,
            max_tokens=1024,
            temperature=0.2,
            system=build_system_prompt("ANNOTATION", persona),
            user_content=user_content,
            final_tool_name="submit_annotation",
            final_tool_description="Submit the intent annotation.",
            final_tool_schema=_ANNOTATION_SCHEMA,
            workspace_path=workspace_path,
        )

        tool_input = self._extract_tool_input(response, "submit_annotation")
        annotation = IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(
                file=task.target_file,
                line=task.target_line,
                method=task.target_function,
            ),
            summary=tool_input.get("summary", ""),
            detail=tool_input.get("detail", ""),
            will_create=tool_input.get("willCreate", []),
            will_modify=tool_input.get("willModify", []),
            side_effect_warnings=tool_input.get("sideEffectWarnings", []),
            assumptions_made=tool_input.get("assumptionsMade", []),
            questions_before_proceeding=tool_input.get("questionsBeforeProceeding", []),
            status=AnnotationStatus.PENDING,
        )
        return annotation

    def execute_task(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        on_chunk: Optional[Callable[[str], None]] = None,
        persona: str = "default",
    ) -> list[dict]:
        """
        Generate code edits for an approved task. Returns list of edit dicts.
        If on_chunk is provided, assistant text is forwarded as it arrives per turn.
        """
        approved = [a for a in task.annotations if a.status == AnnotationStatus.APPROVED]
        annotation_text = "\n\n".join(
            f"ANNOTATION {i+1}:\nSummary: {a.summary}\nDetail: {a.detail}\n"
            f"Modifies: {', '.join(a.will_modify)}\nCreates: {', '.join(a.will_create)}"
            for i, a in enumerate(approved)
        )
        user_content = f"{context}\n\nAPPROVED ANNOTATIONS:\n{annotation_text}"

        final = self._run_structured_tool_turn(
            model=MODEL_EXECUTION,
            max_tokens=8192,
            temperature=0.1,
            system=build_system_prompt("EXECUTION", persona),
            user_content=user_content,
            final_tool_name="submit_edits",
            final_tool_description="Submit the code edits.",
            final_tool_schema=_CODE_EDIT_SCHEMA,
            workspace_path=workspace_path,
            on_text=on_chunk,
        )

        tool_input = self._extract_tool_input(final, "submit_edits")
        return tool_input.get("edits", [])

    def detect_ripple(self, task: Task, scan_context: str, workspace_path: str = "") -> str:
        """
        Analyse the post-execution blast radius described in scan_context.
        Returns a plain-English summary of which callers are at risk.
        Returns empty string if scan_context indicates no changes.
        """
        if "No uncommitted changes" in scan_context:
            return ""
        response = self._run_text_turn(
            model=MODEL_EXECUTION,
            max_tokens=512,
            temperature=0.1,
            system=prompt_templates.RIPPLE_DETECTION,
            user_content=scan_context,
            workspace_path=workspace_path,
        )
        return "\n".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()

    def alter_annotation(
        self,
        task: Task,
        old_annotation: IntentAnnotation,
        feedback: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        """
        Revise an existing annotation based on developer feedback.
        Returns a new IntentAnnotation (old one should be replaced by caller).
        """
        user_content = (
            f"{context}\n\n"
            f"ORIGINAL ANNOTATION:\n"
            f"Summary: {old_annotation.summary}\n"
            f"Detail: {old_annotation.detail}\n"
            f"Will create: {', '.join(old_annotation.will_create)}\n"
            f"Will modify: {', '.join(old_annotation.will_modify)}\n"
            f"Side effects: {', '.join(old_annotation.side_effect_warnings)}\n\n"
            f"DEVELOPER FEEDBACK: {feedback}\n\n"
            f"TASK: {task.title}\n{task.description}"
        )

        response = self._run_structured_tool_turn(
            model=MODEL_EXECUTION,
            max_tokens=1024,
            temperature=0.2,
            system=build_system_prompt("ALTER_ANNOTATION", persona),
            user_content=user_content,
            final_tool_name="submit_annotation",
            final_tool_description="Submit the revised intent annotation.",
            final_tool_schema=_ANNOTATION_SCHEMA,
            workspace_path=workspace_path,
        )

        tool_input = self._extract_tool_input(response, "submit_annotation")
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(
                file=task.target_file,
                line=old_annotation.target_line,
                method=old_annotation.target_function,
            ),
            summary=tool_input.get("summary", ""),
            detail=tool_input.get("detail", ""),
            will_create=tool_input.get("willCreate", []),
            will_modify=tool_input.get("willModify", []),
            side_effect_warnings=tool_input.get("sideEffectWarnings", []),
            assumptions_made=tool_input.get("assumptionsMade", []),
            questions_before_proceeding=tool_input.get("questionsBeforeProceeding", []),
            status=AnnotationStatus.PENDING,
        )

    def analyze_debug_context(self, debug_context: str, workspace_path: str = "", persona: str = "default") -> dict:
        """
        Analyze live debug state and return a structured diagnosis.
        Returns: {diagnosis, likelyCause, suggestedFix, questions}
        """
        _DEBUG_ANALYSIS_SCHEMA = {
            "type": "object",
            "properties": {
                "diagnosis": {
                    "type": "string",
                    "description": "What the variable values and call stack indicate — plain English.",
                },
                "likelyCause": {
                    "type": "string",
                    "description": "Root cause hypothesis based on visible state.",
                },
                "suggestedFix": {
                    "type": "object",
                    "description": "Minimum code change to resolve the issue (as an IntentAnnotation).",
                    "properties": {
                        "summary": {"type": "string"},
                        "detail": {"type": "string"},
                        "targetFile": {"type": "string"},
                        "targetLine": {"type": "integer"},
                        "willModify": {"type": "array", "items": {"type": "string"}},
                        "willCreate": {"type": "array", "items": {"type": "string"}},
                        "sideEffectWarnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["summary", "detail"],
                },
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Things to verify before the fix can be confirmed.",
                },
            },
            "required": ["diagnosis", "likelyCause", "suggestedFix"],
        }

        response = self._run_structured_tool_turn(
            model=MODEL_EXECUTION,
            max_tokens=2048,
            temperature=0.2,
            system=build_system_prompt("LIVE_DEBUG", persona),
            user_content=debug_context,
            final_tool_name="submit_analysis",
            final_tool_description="Submit the debug analysis.",
            final_tool_schema=_DEBUG_ANALYSIS_SCHEMA,
            workspace_path=workspace_path,
        )

        return self._extract_tool_input(response, "submit_analysis")

    def answer_question(self, question: str, context: str, workspace_path: str = "", persona: str = "default") -> dict:
        """
        Answer a developer question during an active session.
        Returns {"text": "...", "planImpact": "..." | None}
        """
        user_content = f"{context}\n\nQUESTION: {question}"

        response = self._run_structured_tool_turn(
            model=MODEL_EXECUTION,
            max_tokens=512,
            temperature=0.4,
            system=build_system_prompt("QUESTION_ANSWER", persona),
            user_content=user_content,
            final_tool_name="submit_answer",
            final_tool_description="Submit the answer.",
            final_tool_schema=_QUESTION_ANSWER_SCHEMA,
            workspace_path=workspace_path,
        )

        return self._extract_tool_input(response, "submit_answer")

    def _run_structured_tool_turn(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        system: str,
        user_content: str,
        final_tool_name: str,
        final_tool_description: str,
        final_tool_schema: dict,
        workspace_path: str = "",
        on_text: Optional[Callable[[str], None]] = None,
    ) -> anthropic.types.Message:
        tools = [*self._host_tools(), _tool(final_tool_name, final_tool_description, final_tool_schema)]
        return self._run_tool_loop(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            user_content=user_content,
            tools=tools,
            tool_choice={"type": "any"},
            final_tool_name=final_tool_name,
            workspace_path=workspace_path,
            on_text=on_text,
        )

    def _run_text_turn(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        system: str,
        user_content: str,
        workspace_path: str = "",
        on_text: Optional[Callable[[str], None]] = None,
    ) -> anthropic.types.Message:
        return self._run_tool_loop(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            user_content=user_content,
            tools=self._host_tools(),
            tool_choice={"type": "auto"},
            final_tool_name="",
            workspace_path=workspace_path,
            on_text=on_text,
        )

    def _run_tool_loop(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        system: str,
        user_content: str,
        tools: list[dict],
        tool_choice: dict,
        final_tool_name: str,
        workspace_path: str,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> anthropic.types.Message:
        self._ensure_graph_workspace(workspace_path)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        last_response: Optional[anthropic.types.Message] = None

        for _ in range(_MAX_TOOL_ROUNDS):
            request: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": messages,
            }
            if tools:
                request["tools"] = tools
                request["tool_choice"] = tool_choice

            response = self._client.messages.create(**request)
            last_response = response

            if on_text:
                for block in response.content:
                    if getattr(block, "type", "") == "text" and getattr(block, "text", ""):
                        on_text(block.text)

            assistant_content = [self._serialize_block(block) for block in response.content]
            messages.append({"role": "assistant", "content": assistant_content})

            tool_uses = [block for block in response.content if getattr(block, "type", "") == "tool_use"]
            if not tool_uses:
                if final_tool_name:
                    raise RuntimeError(f"Claude did not call required tool '{final_tool_name}'")
                return response

            if final_tool_name and any(block.name == final_tool_name for block in tool_uses):
                return response

            tool_results = []
            for block in tool_uses:
                result = self._execute_host_tool(
                    name=block.name,
                    tool_input=block.input,
                    workspace_path=workspace_path,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=True),
                        "is_error": isinstance(result, dict) and result.get("error") is not None,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        if final_tool_name:
            raise RuntimeError(
                f"Claude exceeded {_MAX_TOOL_ROUNDS} tool rounds without calling '{final_tool_name}'"
            )
        if last_response is None:
            raise RuntimeError("Claude did not return a response")
        return last_response

    def _graph_tools(self) -> list[dict]:
        if not self._graph:
            return []

        return [
            _tool(
                "index_repository",
                "Index or refresh a repository so graph queries use the current codebase state.",
                {
                    "type": "object",
                    "properties": {"repoPath": {"type": "string"}},
                    "required": ["repoPath"],
                },
            ),
            _tool(
                "list_projects",
                "List indexed graph projects available to query.",
                {"type": "object", "properties": {}},
            ),
            _tool(
                "index_status",
                "Get index readiness for a repository or project.",
                {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "repoPath": {"type": "string"},
                    },
                },
            ),
            _tool(
                "get_graph_schema",
                "Inspect the current graph schema, node labels, edge types, and relationship patterns.",
                {
                    "type": "object",
                    "properties": {"project": {"type": "string"}, "repoPath": {"type": "string"}},
                },
            ),
            _tool(
                "get_architecture",
                "Get architecture summaries such as entry points, hotspots, clusters, layers, and ADR.",
                {
                    "type": "object",
                    "properties": {
                        "aspects": {"type": "array", "items": {"type": "string"}},
                        "repoPath": {"type": "string"},
                    },
                },
            ),
            _tool(
                "search_graph",
                "Find symbols in the indexed graph by name, qualified name, file pattern, label, or graph degree.",
                {
                    "type": "object",
                    "properties": {
                        "namePattern": {"type": "string"},
                        "qnPattern": {"type": "string"},
                        "filePattern": {"type": "string"},
                        "label": {"type": "string"},
                        "relationship": {"type": "string"},
                        "direction": {"type": "string", "enum": ["any", "inbound", "outbound"]},
                        "minDegree": {"type": "integer"},
                        "maxDegree": {"type": "integer"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                        "caseSensitive": {"type": "boolean"},
                        "project": {"type": "string"},
                        "repoPath": {"type": "string"},
                    },
                },
            ),
            _tool(
                "search_code",
                "Run indexed code search across workspace files.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "regex": {"type": "boolean"},
                        "caseSensitive": {"type": "boolean"},
                        "maxResults": {"type": "integer"},
                        "offset": {"type": "integer"},
                        "filePattern": {"type": "string"},
                        "repoPath": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            ),
            _tool(
                "find_qualified_name",
                "Resolve a short symbol name to a qualified name in the current graph.",
                {
                    "type": "object",
                    "properties": {"shortName": {"type": "string"}, "repoPath": {"type": "string"}},
                    "required": ["shortName"],
                },
            ),
            _tool(
                "get_code_snippet",
                "Fetch source and metadata for a symbol from the graph index.",
                {
                    "type": "object",
                    "properties": {
                        "qualifiedName": {"type": "string"},
                        "autoResolve": {"type": "boolean"},
                        "includeNeighbors": {"type": "boolean"},
                        "repoPath": {"type": "string"},
                    },
                    "required": ["qualifiedName"],
                },
            ),
            _tool(
                "trace_call_path",
                "Trace inbound or outbound call paths for a function.",
                {
                    "type": "object",
                    "properties": {
                        "functionName": {"type": "string"},
                        "direction": {"type": "string", "enum": ["both", "inbound", "outbound"]},
                        "depth": {"type": "integer"},
                        "riskLabels": {"type": "boolean"},
                        "minConfidence": {"type": "number"},
                        "repoPath": {"type": "string"},
                    },
                    "required": ["functionName"],
                },
            ),
            _tool(
                "detect_changes",
                "Map changed files and symbols to impacted callers.",
                {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string"},
                        "depth": {"type": "integer"},
                        "repoPath": {"type": "string"},
                    },
                },
            ),
            _tool(
                "query_graph",
                "Run a read-only graph query against the indexed codebase.",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "repoPath": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ]

    def _knowledge_tools(self) -> list[dict]:
        return [
            _tool(
                "search_knowledge",
                "Search the global snippet store for reusable patterns and examples.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            ),
            _tool(
                "list_knowledge_sources",
                "List snippetized repositories available in the global snippet store.",
                {"type": "object", "properties": {}},
            ),
        ]

    def _task_tools(self) -> list[dict]:
        return [
            _tool(
                "list_tasks",
                "List durable workspace backlog tasks from `.waterfree/tasks.db`.",
                {
                    "type": "object",
                    "properties": {
                        "workspacePath": {"type": "string"},
                        "status": {"type": "string"},
                        "ownerName": {"type": "string"},
                        "ownerType": {"type": "string"},
                        "priority": {"type": "string"},
                        "phase": {"type": "string"},
                        "readyOnly": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                },
            ),
            _tool(
                "search_tasks",
                "Search the durable workspace backlog by title, description, or target path.",
                {
                    "type": "object",
                    "properties": {
                        "workspacePath": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            ),
            _tool(
                "add_task",
                "Add a task to the durable workspace backlog.",
                {
                    "type": "object",
                    "properties": {
                        "workspacePath": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "rationale": {"type": "string"},
                        "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3", "spike"]},
                        "phase": {"type": "string"},
                        "taskType": {"type": "string", "enum": ["impl", "test", "spike", "review", "refactor"]},
                        "status": {"type": "string", "enum": ["pending", "annotating", "negotiating", "executing", "complete", "skipped"]},
                        "owner": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["human", "agent", "unassigned"]},
                                "name": {"type": "string"},
                            },
                        },
                        "targetCoord": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "class": {"type": "string"},
                                "method": {"type": "string"},
                                "line": {"type": "integer"},
                                "anchorType": {
                                    "type": "string",
                                    "enum": ["create-at", "modify", "delete", "read-only-context"],
                                },
                            },
                        },
                    },
                    "required": ["title"],
                },
            ),
            _tool(
                "update_task",
                "Update a durable workspace backlog task.",
                {
                    "type": "object",
                    "properties": {
                        "workspacePath": {"type": "string"},
                        "taskId": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                    "required": ["taskId", "patch"],
                },
            ),
            _tool(
                "delete_task",
                "Delete a task from the durable workspace backlog.",
                {
                    "type": "object",
                    "properties": {
                        "workspacePath": {"type": "string"},
                        "taskId": {"type": "string"},
                    },
                    "required": ["taskId"],
                },
            ),
            _tool(
                "what_next",
                "Return the highest-priority ready backlog task for an owner.",
                {
                    "type": "object",
                    "properties": {
                        "workspacePath": {"type": "string"},
                        "ownerName": {"type": "string"},
                        "includeUnassigned": {"type": "boolean"},
                    },
                },
            ),
        ]

    def _host_tools(self) -> list[dict]:
        return [*self._graph_tools(), *self._task_tools(), *self._knowledge_tools()]

    def _ensure_graph_workspace(self, workspace_path: str) -> None:
        if not self._graph or not workspace_path:
            return
        try:
            self._graph.index_status(repo_path=workspace_path)
        except Exception as exc:
            log.debug("Graph workspace selection failed for %s: %s", workspace_path, exc)

    def _get_task_store(self, workspace_path: str) -> Optional[TaskStore]:
        if not workspace_path:
            return None
        path = str(Path(workspace_path).resolve())
        if path not in self._task_stores:
            self._task_stores[path] = self._task_store_factory(path)
        return self._task_stores[path]

    def _get_knowledge_store(self) -> KnowledgeStore:
        if not self._knowledge_store:
            self._knowledge_store = KnowledgeStore()
        return self._knowledge_store

    def _execute_host_tool(self, name: str, tool_input: dict, workspace_path: str) -> dict:
        if name in {
            "index_repository",
            "list_projects",
            "index_status",
            "get_graph_schema",
            "get_architecture",
            "search_graph",
            "search_code",
            "find_qualified_name",
            "get_code_snippet",
            "trace_call_path",
            "detect_changes",
            "query_graph",
        }:
            return self._execute_graph_tool(name, tool_input, workspace_path)

        if name in {"list_tasks", "search_tasks", "add_task", "update_task", "delete_task", "what_next"}:
            store = self._get_task_store(str(tool_input.get("workspacePath", "") or workspace_path))
            if not store:
                return {"error": "Task store unavailable", "tool": name}
            try:
                if name == "list_tasks":
                    data = store.list_tasks(
                        status=str(tool_input.get("status", "")),
                        owner_name=str(tool_input.get("ownerName", "")),
                        owner_type=str(tool_input.get("ownerType", "")),
                        priority=str(tool_input.get("priority", "")),
                        phase=str(tool_input.get("phase", "")),
                        ready_only=bool(tool_input.get("readyOnly", False)),
                        limit=int(tool_input.get("limit", 100)),
                    )
                    payload = data.to_dict()
                    payload["path"] = store.path
                    return payload
                if name == "search_tasks":
                    tasks = store.search_tasks(
                        query=str(tool_input.get("query", "")),
                        limit=int(tool_input.get("limit", 20)),
                    )
                    return {"tasks": [task.to_dict() for task in tasks], "count": len(tasks), "path": store.path}
                if name == "add_task":
                    payload = {k: v for k, v in tool_input.items() if k != "workspacePath"}
                    task = store.add_task(payload)
                    return {"task": task.to_dict(), "path": store.path}
                if name == "update_task":
                    task = store.update_task(
                        str(tool_input.get("taskId", "")),
                        dict(tool_input.get("patch", {})),
                    )
                    return {"task": task.to_dict(), "path": store.path}
                if name == "delete_task":
                    deleted = store.delete_task(str(tool_input.get("taskId", "")))
                    return {"ok": True, "deleted": deleted, "path": store.path}
                if name == "what_next":
                    task = store.get_next_task(
                        owner_name=str(tool_input.get("ownerName", "")),
                        include_unassigned=bool(tool_input.get("includeUnassigned", True)),
                    )
                    return {"task": task.to_dict() if task else None, "path": store.path}
            except Exception as exc:
                log.warning("Task tool %s failed: %s", name, exc)
                return {"error": str(exc), "tool": name}

        if name in {"search_knowledge", "list_knowledge_sources"}:
            store = self._get_knowledge_store()
            try:
                if name == "search_knowledge":
                    entries = store.search(str(tool_input.get("query", "")), limit=int(tool_input.get("limit", 10)))
                    return {
                        "entries": [entry.to_dict() for entry in entries],
                        "count": len(entries),
                        "total": store.total_entries(),
                    }
                if name == "list_knowledge_sources":
                    repos = store.list_repos()
                    return {
                        "repos": [repo.to_dict() for repo in repos],
                        "totalEntries": store.total_entries(),
                    }
            except Exception as exc:
                log.warning("Knowledge tool %s failed: %s", name, exc)
                return {"error": str(exc), "tool": name}

        return {"error": f"Unsupported tool: {name}"}

    def _execute_graph_tool(self, name: str, tool_input: dict, workspace_path: str) -> dict:
        if not self._graph:
            return {"error": "Graph tools are unavailable"}

        repo_path = str(tool_input.get("repoPath", "") or workspace_path)
        if name != "index_repository":
            self._ensure_graph_workspace(repo_path)

        try:
            if name == "index_repository":
                return self._graph.index(repo_path)
            if name == "list_projects":
                return self._graph.list_projects()
            if name == "index_status":
                return self._graph.index_status(
                    project=str(tool_input.get("project", "")),
                    repo_path=repo_path,
                )
            if name == "get_graph_schema":
                if repo_path:
                    self._ensure_graph_workspace(repo_path)
                return self._graph.get_graph_schema(project=str(tool_input.get("project", "")))
            if name == "get_architecture":
                return self._graph.get_architecture(aspects=tool_input.get("aspects"))
            if name == "search_graph":
                return self._graph.search_graph(
                    name_pattern=str(tool_input.get("namePattern", "")),
                    qn_pattern=str(tool_input.get("qnPattern", "")),
                    file_pattern=str(tool_input.get("filePattern", "")),
                    label=str(tool_input.get("label", "")),
                    relationship=str(tool_input.get("relationship", "")),
                    direction=str(tool_input.get("direction", "any")),
                    min_degree=int(tool_input.get("minDegree", 0)),
                    max_degree=int(tool_input.get("maxDegree", -1)),
                    limit=int(tool_input.get("limit", 10)),
                    offset=int(tool_input.get("offset", 0)),
                    case_sensitive=bool(tool_input.get("caseSensitive", False)),
                    project=str(tool_input.get("project", "")),
                )
            if name == "search_code":
                return self._graph.search_code(
                    pattern=str(tool_input.get("pattern", "")),
                    regex=bool(tool_input.get("regex", False)),
                    case_sensitive=bool(tool_input.get("caseSensitive", False)),
                    max_results=int(tool_input.get("maxResults", 50)),
                    offset=int(tool_input.get("offset", 0)),
                    file_pattern=str(tool_input.get("filePattern", "")),
                )
            if name == "find_qualified_name":
                return {"qualifiedName": self._graph.find_qualified_name(str(tool_input.get("shortName", "")))}
            if name == "get_code_snippet":
                return self._graph.get_code_snippet(
                    qualified_name=str(tool_input.get("qualifiedName", "")),
                    auto_resolve=bool(tool_input.get("autoResolve", True)),
                    include_neighbors=bool(tool_input.get("includeNeighbors", False)),
                )
            if name == "trace_call_path":
                return self._graph.trace_call_path(
                    function_name=str(tool_input.get("functionName", "")),
                    direction=str(tool_input.get("direction", "both")),
                    depth=int(tool_input.get("depth", 3)),
                    risk_labels=bool(tool_input.get("riskLabels", False)),
                    min_confidence=float(tool_input.get("minConfidence", 0.0)),
                )
            if name == "detect_changes":
                return self._graph.detect_changes(
                    scope=str(tool_input.get("scope", "all")),
                    depth=int(tool_input.get("depth", 3)),
                )
            if name == "query_graph":
                return self._graph.query_graph(str(tool_input.get("query", "")))
        except Exception as exc:
            log.warning("Graph tool %s failed: %s", name, exc)
            return {"error": str(exc), "tool": name}

        return {"error": f"Unsupported tool: {name}"}

    @staticmethod
    def _serialize_block(block: Any) -> dict[str, Any]:
        if hasattr(block, "model_dump"):
            return block.model_dump(exclude_none=True)
        out: dict[str, Any] = {"type": getattr(block, "type", "text")}
        for field in ("id", "name", "input", "text"):
            value = getattr(block, field, None)
            if value is not None:
                out[field] = value
        return out

    @staticmethod
    def _extract_tool_input(response: anthropic.types.Message, tool_name: str) -> dict:
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        log.error("Tool '%s' not found in response content: %s", tool_name, response.content)
        return {}
