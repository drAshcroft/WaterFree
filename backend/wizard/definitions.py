from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class ChunkDef:
    id: str
    title: str
    guidance: str
    required: bool = True


@dataclass(frozen=True)
class StageTemplate:
    id: str
    kind: str
    title: str
    persona: str
    relative_doc_path: str
    chunks: tuple[ChunkDef, ...]


MARKET_RESEARCH_TEMPLATE = StageTemplate(
    id="market_research",
    kind="market_research",
    title="Market Research",
    persona="market_researcher",
    relative_doc_path="market-research.md",
    chunks=(
        ChunkDef("initial_goal", "What is the idea?", "Describe the software idea, problem you want to solve or frustration in plain language."),
        ChunkDef("similar_ideas", "Similar Ideas and Niches", "Compare the idea to adjacent products and call out what feels different."),
        ChunkDef("who_wants_this", "Who Wants This?", "Describe the primary audiences and their pain points."),
        ChunkDef("core_features", "Core Features", "List the features that make the idea compelling."),
        ChunkDef("minimum_viable_product", "Minimum Viable Product", "Define the smallest shippable version."),
        ChunkDef("feature_list", "Feature List", "Capture the broader feature backlog after the MVP."),
        ChunkDef("stretch_goals", "Stretch Goals", "Note the ambitious features that should not block v1."),
    ),
)

ARCHITECT_TEMPLATE = StageTemplate(
    id="architect_review",
    kind="architect_review",
    title="Architect Review",
    persona="architect",
    relative_doc_path="architect.md",
    chunks=(
        ChunkDef("comparable_architectures", "Comparable Architectures", "Outline credible implementation shapes and trade-offs."),
        ChunkDef("tech_stack", "Tech Stack", "Recommend the stack and explain why it fits."),
        ChunkDef("system_constraints", "System Constraints", "List constraints that must shape the design."),
        ChunkDef("data_model_storage", "Data Model and Storage", "Describe the core data model and storage choices."),
        ChunkDef("security_concerns", "Security Concerns", "Call out security-sensitive flows and controls."),
        ChunkDef("privacy_policy", "Privacy and Policy", "Highlight privacy, compliance, and policy concerns."),
        ChunkDef("scalability_targets", "Scalability Targets", "State scaling assumptions and performance limits."),
        ChunkDef("deployment_model", "Deployment Model", "Describe how the system should be deployed and operated."),
        ChunkDef("top_level_architecture", "Top Level Architecture", "Lay out the major subsystems and interfaces."),
        ChunkDef("development_roadmap", "Development Roadmap", "Break delivery into a realistic roadmap."),
        ChunkDef("reality_check", "Reality Check", "Challenge weak assumptions and call out the risky parts."),
    ),
)

BDD_TEMPLATE = StageTemplate(
    id="bdd_ai_tests",
    kind="bdd_ai_tests",
    title="BDD AI Tests",
    persona="bdd_test_designer",
    relative_doc_path="bdd-tests.md",
    chunks=(
        ChunkDef("acceptance_scenarios", "Acceptance Scenarios", "Describe human-readable behaviors that prove the product works."),
        ChunkDef("edge_case_matrix", "Edge Case Matrix", "Identify important failures, abuse cases, and edge conditions."),
        ChunkDef("test_prompts", "BDD Prompts", "Turn the scenarios into implementation-ready test prompts."),
    ),
)

CODING_TEMPLATE = StageTemplate(
    id="coding_agents",
    kind="coding_agents",
    title="Coding Agents",
    persona="coding_agent",
    relative_doc_path="coding-handoff.md",
    chunks=(
        ChunkDef("implementation_scope", "Implementation Scope", "Summarize the accepted implementation boundaries."),
        ChunkDef("execution_order", "Execution Order", "Explain the task order the coding lane should follow."),
        ChunkDef("build_tasks", "Build Tasks", "List the concrete coding tasks that will enter the session."),
        ChunkDef("testing_handoff", "Testing Handoff", "State how BDD outputs map onto implementation and verification."),
    ),
)

REVIEW_TEMPLATE = StageTemplate(
    id="review",
    kind="review",
    title="Review",
    persona="reviewer",
    relative_doc_path="review.md",
    chunks=(
        ChunkDef("execution_summary", "Execution Summary", "Summarize what was completed and what remains."),
        ChunkDef("open_issues", "Open Issues", "List unresolved defects, risks, and weak spots."),
        ChunkDef("blocked_procedures", "Blocked Procedures", "Call out work that could not be completed cleanly."),
        ChunkDef("follow_up_tasks", "Follow-up Tasks", "Route the remaining work back into backlog items."),
    ),
)


def make_design_template(subsystem_name: str) -> StageTemplate:
    slug = slugify(subsystem_name)
    title = f"Design Pattern Agent - {subsystem_name}"
    return StageTemplate(
        id=f"design:{slug}",
        kind="design_pattern_agent",
        title=title,
        persona="pattern_expert",
        relative_doc_path=f"design/{slug}.md",
        chunks=(
            ChunkDef("similar_subsystems", "Similar Subsystems", "Compare the subsystem to analogous designs or services."),
            ChunkDef("third_party_apis", "3rd Party APIs", "List the external APIs, services, or libraries involved."),
            ChunkDef("subsystem_overview", "Subsystem Overview", "Describe the subsystem's purpose and boundaries."),
            ChunkDef("communication_patterns", "Communication Patterns", "Describe how data and control move through the subsystem."),
            ChunkDef("placement", "Placement", "State where the subsystem belongs in the product architecture."),
            ChunkDef("interfaces", "Interfaces", "Define the main interfaces, contracts, and ownership boundaries."),
            ChunkDef("data_contracts", "Data Contracts", "Describe schemas, payloads, and validation rules."),
            ChunkDef("logging", "Logging", "Describe observability and logging expectations."),
            ChunkDef("failure_handling", "Failure Handling", "Explain failures, retries, and degraded behavior."),
            ChunkDef("subsystem_todos", "Subsystem Todos", "Break the subsystem into actionable follow-up work."),
        ),
    )


def make_wireframe_template(subsystem_name: str) -> StageTemplate:
    slug = slugify(subsystem_name)
    title = f"Wireframe Agents - {subsystem_name}"
    return StageTemplate(
        id=f"wireframe:{slug}",
        kind="wireframe_agents",
        title=title,
        persona="stub_wireframer",
        relative_doc_path=f"wireframes/{slug}.md",
        chunks=(
            ChunkDef("file_skeleton_plan", "File Skeleton Plan", "List the files and shells needed for this subsystem."),
            ChunkDef("interface_outline", "Interface Outline", "Show the high-level interface and wiring shape."),
            ChunkDef("pseudocode", "Pseudocode", "Describe the core flows in structured pseudocode."),
            ChunkDef("micro_prompts", "Micro Prompts", "Produce precise coding prompts for the downstream coding lane."),
        ),
    )


def stage_template_for_stage_id(stage_id: str, subsystem_name: str = "") -> StageTemplate:
    if stage_id == MARKET_RESEARCH_TEMPLATE.id:
        return MARKET_RESEARCH_TEMPLATE
    if stage_id == ARCHITECT_TEMPLATE.id:
        return ARCHITECT_TEMPLATE
    if stage_id == BDD_TEMPLATE.id:
        return BDD_TEMPLATE
    if stage_id == CODING_TEMPLATE.id:
        return CODING_TEMPLATE
    if stage_id == REVIEW_TEMPLATE.id:
        return REVIEW_TEMPLATE
    if stage_id.startswith("design:"):
        return make_design_template(subsystem_name or stage_id.split(":", 1)[1].replace("-", " "))
    if stage_id.startswith("wireframe:"):
        return make_wireframe_template(subsystem_name or stage_id.split(":", 1)[1].replace("-", " "))
    raise ValueError(f"Unknown stage id: {stage_id}")


def wizard_root(workspace_path: str, run_id: str) -> Path:
    return Path(workspace_path).resolve() / ".waterfree" / "wizards" / run_id


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return slug.strip("-") or "subsystem"
