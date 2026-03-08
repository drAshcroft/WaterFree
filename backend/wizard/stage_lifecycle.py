from __future__ import annotations

from backend.wizard.definitions import (
    ARCHITECT_TEMPLATE,
    BDD_TEMPLATE,
    CODING_TEMPLATE,
    REVIEW_TEMPLATE,
    StageTemplate,
    make_design_template,
    make_wireframe_template,
)
from backend.wizard.models import (
    WizardRun,
    WizardRunStatus,
    WizardStageState,
    WizardStageStatus,
)
from backend.wizard.stage_executor import _phase_for_stage


def is_stage_unlocked(run: WizardRun, stage: WizardStageState) -> bool:
    stage_phase = _phase_for_stage(stage.id)
    for other in run.stages:
        if _phase_for_stage(other.id) < stage_phase and other.status != WizardStageStatus.ACCEPTED:
            return False
    return True


def recompute_current_stage(run: WizardRun) -> None:
    for stage in run.stages:
        if stage.status != WizardStageStatus.ACCEPTED and is_stage_unlocked(run, stage):
            run.current_stage_id = stage.id
            return
    run.current_stage_id = run.stages[-1].id if run.stages else ""


def all_accepted(run: WizardRun, *, prefix: str) -> bool:
    relevant = [stage for stage in run.stages if stage.id.startswith(prefix)]
    return bool(relevant) and all(stage.status == WizardStageStatus.ACCEPTED for stage in relevant)


def on_stage_accepted(
    run: WizardRun,
    stage: WizardStageState,
    ensure_stage_doc_fn,
    stage_from_template_fn,
) -> None:
    """Trigger post-acceptance side-effects: unlock new stages, transition run status."""
    if stage.id == ARCHITECT_TEMPLATE.id:
        subsystems = [
            str(item).strip()
            for item in stage.derived_artifacts.get("subsystems", [])
            if str(item).strip()
        ]
        if not subsystems:
            subsystems = ["Core Application"]
        _ensure_design_stages(run, subsystems, ensure_stage_doc_fn, stage_from_template_fn)
        return

    if stage.id.startswith("design:") and all_accepted(run, prefix="design:"):
        subsystem_names = [s.subsystem_name for s in run.stages if s.id.startswith("design:")]
        _ensure_wireframe_stages(run, subsystem_names, ensure_stage_doc_fn, stage_from_template_fn)
        return

    if stage.id.startswith("wireframe:") and all_accepted(run, prefix="wireframe:"):
        ensure_static_stage(run, BDD_TEMPLATE, ensure_stage_doc_fn, stage_from_template_fn)
        return

    if stage.id == BDD_TEMPLATE.id:
        ensure_static_stage(run, CODING_TEMPLATE, ensure_stage_doc_fn, stage_from_template_fn)
        return

    if stage.id == CODING_TEMPLATE.id:
        ensure_static_stage(run, REVIEW_TEMPLATE, ensure_stage_doc_fn, stage_from_template_fn)
        return

    if stage.id == REVIEW_TEMPLATE.id:
        run.status = WizardRunStatus.COMPLETE


def ensure_static_stage(
    run: WizardRun,
    template: StageTemplate,
    ensure_stage_doc_fn,
    stage_from_template_fn,
) -> WizardStageState:
    existing = run.get_stage(template.id)
    if existing:
        return existing
    stage = stage_from_template_fn(run.id, template)
    run.stages.append(stage)
    ensure_stage_doc_fn(run, stage)
    return stage


def _ensure_design_stages(
    run: WizardRun,
    subsystems: list[str],
    ensure_stage_doc_fn,
    stage_from_template_fn,
) -> None:
    for subsystem_name in subsystems:
        template = make_design_template(subsystem_name)
        if run.get_stage(template.id):
            continue
        stage = stage_from_template_fn(run.id, template, subsystem_name=subsystem_name)
        run.stages.append(stage)
        ensure_stage_doc_fn(run, stage)


def _ensure_wireframe_stages(
    run: WizardRun,
    subsystem_names: list[str],
    ensure_stage_doc_fn,
    stage_from_template_fn,
) -> None:
    for subsystem_name in subsystem_names:
        template = make_wireframe_template(subsystem_name)
        if run.get_stage(template.id):
            continue
        stage = stage_from_template_fn(run.id, template, subsystem_name=subsystem_name)
        run.stages.append(stage)
        ensure_stage_doc_fn(run, stage)
