/**
 * CommandRegistry — registers all VS Code commands and wires them to WaterFreeController.
 */

import * as vscode from "vscode";
import type { WaterFreeController } from "../extension.js";

export class CommandRegistry {
  constructor(private readonly controller: WaterFreeController) {}

  register(context: vscode.ExtensionContext): void {
    const c = this.controller;
    const register = (id: string, fn: (...args: unknown[]) => unknown) =>
      context.subscriptions.push(vscode.commands.registerCommand(id, fn));

    register("waterfree.start", () => c.cmdStart());
    register("waterfree.setup", () => c.cmdSetup());
    register("waterfree.indexWorkspace", () => c.cmdIndex());
    register("waterfree.generateAnnotation", (taskId: unknown) =>
      c.cmdGenerateAnnotation(String(taskId)),
    );
    register("waterfree.approveAnnotation", (annotationId: unknown) =>
      c.cmdApprove(String(annotationId)),
    );
    register("waterfree.alterAnnotation", (taskId: unknown, annotationId: unknown) =>
      c.cmdAlter(String(taskId), String(annotationId)),
    );
    register("waterfree.redirectTask", (taskId: unknown) =>
      c.cmdRedirect(String(taskId)),
    );
    register("waterfree.skipTask", (taskId: unknown) =>
      c.cmdSkipTask(String(taskId)),
    );
    register("waterfree.showAnnotation", (taskId: unknown, annotationId?: unknown) =>
      c.cmdShowAnnotation(String(taskId), annotationId ? String(annotationId) : undefined),
    );
    register("waterfree.openSidebar", () => {
      void vscode.commands.executeCommand("waterfree.planSidebar.focus");
    });
    register("waterfree.livePairDebug", () => c.cmdLivePairDebug());
    register("waterfree.pushDebugToAgent", () => c.cmdPushDebugToAgent());
    register("waterfree.quickAction", (actionId: unknown) =>
      c.getQuickActions().runAction(String(actionId)),
    );
    register("waterfree.buildKnowledge", () => c.cmdBuildKnowledge());
    register("waterfree.addKnowledgeRepo", () => c.cmdAddKnowledgeRepo());
    register("waterfree.openTodoBoard", () => c.cmdOpenTodoBoard());
    register("waterfree.openKnowledge", () => c.cmdOpenKnowledgePanel());
    register("waterfree.extractProcedure", () => c.cmdExtractProcedure());
    register("waterfree.openWizard", (args?: unknown) => c.cmdOpenWizard(args));
    register("waterfree.runWizardStep", (ctx?: unknown) => c.cmdRunWizardStep(ctx));
    register("waterfree.acceptWizardChunk", (ctx?: unknown, chunkId?: unknown) =>
      c.cmdAcceptWizardChunk(ctx, chunkId),
    );
    register("waterfree.reviseWizardChunk", (ctx?: unknown, chunkId?: unknown) =>
      c.cmdReviseWizardChunk(ctx, chunkId),
    );
    register("waterfree.acceptWizardStep", (ctx?: unknown) => c.cmdAcceptWizardStep(ctx));
    register("waterfree.promoteWizardTodos", (ctx?: unknown) => c.cmdPromoteWizardTodos(ctx));
    register("waterfree.startWizardCoding", (ctx?: unknown) => c.cmdStartWizardCoding(ctx));
    register("waterfree.runWizardReview", (ctx?: unknown) => c.cmdRunWizardReview(ctx));
    register("waterfree.refineWizardIdea", (ctx?: unknown) => c.cmdRefineWizardIdea(ctx));
    register("waterfree.openMonitorPanel", () => c.cmdOpenMonitorPanel());
  }
}
