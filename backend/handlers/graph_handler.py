"""Graph/ADR/project management handlers."""
from __future__ import annotations


def handle_get_adr(server, params: dict) -> dict:
    sections = params.get("sections")
    kwargs = {}
    if sections:
        kwargs["include"] = sections
    return server._graph.manage_adr("get", **kwargs)


def handle_store_adr(server, params: dict) -> dict:
    content = params.get("content", "")
    if not content:
        raise ValueError("content is required")
    return server._graph.manage_adr("store", content=content)


def handle_update_adr(server, params: dict) -> dict:
    sections = params.get("sections", {})
    if not sections:
        raise ValueError("sections is required")
    return server._graph.manage_adr("update", sections=sections)


def handle_delete_adr(server, params: dict) -> dict:
    return server._graph.manage_adr("delete")


def handle_list_projects(server, params: dict) -> dict:
    return server._graph.list_projects()


def handle_delete_project(server, params: dict) -> dict:
    return server._graph.delete_project(
        project=params.get("project", ""),
        repo_path=params.get("repoPath", ""),
    )


def handle_index_status(server, params: dict) -> dict:
    return server._graph.index_status(
        project=params.get("project", ""),
        repo_path=params.get("repoPath", ""),
    )


def handle_get_graph_schema(server, params: dict) -> dict:
    return server._graph.get_graph_schema(project=params.get("project", ""))
