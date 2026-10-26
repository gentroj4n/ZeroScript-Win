// SPDX-License-Identifier: GPL-3.0-or-later
"use strict";
const assert = require("node:assert/strict");
const L = require("./autopilot-ledger.js");

const run = L.createRun({
  goal: "Implement a sample feature safely.",
  workspace: "C:/projects/sample",
  acceptanceCriteria: ["Feature is implemented", "Relevant tests pass"],
  maxRunMs: 300000,
});
assert.equal(run.tasks.length, 8);
assert.equal(run.state, "planning");
assert.throws(() => L.createRun({ goal: "missing criteria" }), /acceptance criterion/);

let ready = L.refreshReady(run);
assert.deepEqual(ready.map((t) => t.role), ["project_manager"]);
let claim = L.claimNext(run, { id: "worker-pm", provider: "deepseek", tabId: 1, roles: ["project_manager"] });
assert.equal(claim.task.role, "project_manager");
assert.equal(claim.task.status, "leased");
assert.throws(() => L.transition(run, claim.task.id, "running", { leaseToken: "wrong" }), /lease token/);
L.transition(run, claim.task.id, "running", { leaseToken: claim.lease.token });
L.transition(run, claim.task.id, "completed", {
  evidence: [{ summary: "Plan written and acceptance criteria enumerated", source: "worker report" }],
});
ready = L.refreshReady(run);
assert.deepEqual(ready.map((t) => t.role).sort(), ["ideas_insights", "implementation"]);

const impl = L.claimNext(run, { id: "worker-impl", provider: "gemini", tabId: 2, roles: ["implementation"] });
L.transition(run, impl.task.id, "running", { leaseToken: impl.lease.token });
L.transition(run, impl.task.id, "completed", {
  evidence: [{ summary: "Changed src/feature.js", source: "diff" }],
});
ready = L.refreshReady(run);
assert(ready.some((t) => t.role === "qa"));
assert(ready.some((t) => t.role === "code_review"));
assert(ready.some((t) => t.role === "security"));

const qa = run.tasks.find((t) => t.role === "qa");
const qaClaim = L.claimNext(run, { id: "qa-worker", provider: "chatgpt", roles: ["qa"] });
L.transition(run, qa.id, "running", { leaseToken: qaClaim.lease.token });
assert.throws(() => L.transition(run, qa.id, "completed", {
  evidence: [{ summary: "looks fine", tool: "workspace_run_task", output: "12 passed" }],
  criteriaVerified: ["Feature is implemented", "Relevant tests pass"],
}), /structured evidence/);
assert.throws(() => L.transition(run, qa.id, "completed", {
  evidence: [{ summary: "Test output", tool: "workspace_run_task", output: "12 passed" }],
  criteriaEvidence: [
    { criterion: "Feature is implemented", status: "VERIFIED", tool: "workspace_run_task", excerpt: "invented proof" },
    { criterion: "Relevant tests pass", status: "VERIFIED", tool: "workspace_run_task", excerpt: "12 passed" },
  ],
}), /must match an observed successful tool output/);
L.transition(run, qa.id, "completed", {
  evidence: [{ summary: "Test output: 12 passed", tool: "workspace_run_task", output: "Test output: 12 passed" }],
  criteriaEvidence: [
    { criterion: "Feature is implemented", status: "VERIFIED", tool: "workspace_run_task", excerpt: "Test output" },
    { criterion: "Relevant tests pass", status: "VERIFIED", tool: "workspace_run_task", excerpt: "12 passed" },
  ],
});
assert.equal(qa.criteriaVerified.length, 2);
assert.equal(qa.criteriaEvidence.length, 2);
assert.equal(Object.hasOwn(qa.evidence[0], "output"), false, "raw tool output must not be retained in durable storage");
assert.equal(qa.criteriaEvidence[1].excerpt, "12 passed");

const leaseRun = L.createRun({ goal: "Test lease renewal", acceptanceCriteria: ["Done"] });
L.refreshReady(leaseRun);
const leaseClaim = L.claimNext(leaseRun, { id: "heartbeat-worker" });
const previousExpiry = leaseClaim.lease.expiresAt;
const renewed = L.renewLease(leaseRun, leaseClaim.task.id, leaseClaim.lease.token, 600000);
assert(Date.parse(renewed.expiresAt) > Date.parse(previousExpiry));
assert.throws(() => L.renewLease(leaseRun, leaseClaim.task.id, "stale-token"), /invalid/);

const run2 = L.createRun({ goal: "Test cancellation", acceptanceCriteria: ["Done"] });
L.refreshReady(run2);
L.cancelRun(run2, "user clicked stop");
assert.equal(run2.state, "cancelled");
assert(run2.tasks.every((t) => !["queued", "ready", "leased", "running"].includes(t.status)));

const run3 = L.createRun({ goal: "Test lease recovery", acceptanceCriteria: ["Done"] });
L.refreshReady(run3);
const lease = L.claimNext(run3, { id: "lost-worker", leaseMs: 30000 });
lease.task.lease.expiresAt = new Date(Date.now() - 1000).toISOString();
assert.deepEqual(L.recoverExpiredLeases(run3), [lease.task.id]);
assert.equal(lease.task.status, "ready");

const partial = L.createRun({ goal: "One branch blocks, another can proceed", acceptanceCriteria: ["Done"] });
L.refreshReady(partial);
const pm = L.claimNext(partial, { id: "pm" });
L.transition(partial, pm.task.id, "running", { leaseToken: pm.lease.token });
L.transition(partial, pm.task.id, "completed", { evidence: [{ summary: "Plan reviewed" }] });
L.refreshReady(partial);
const impl2 = L.claimNext(partial, { id: "impl", roles: ["implementation"] });
L.transition(partial, impl2.task.id, "running", { leaseToken: impl2.lease.token });
L.transition(partial, impl2.task.id, "blocked", { reason: "Implementation blocked" });
L.refreshReady(partial);
assert(partial.tasks.some((t) => t.role === "ideas_insights" && t.status === "ready"));
assert.equal(L.finalize(partial), "verifying", "independent ready tasks must remain schedulable after a sibling task blocks");

// The current architecture shares one workspace across browser-session workers.
// A global writer lock must serialize implementation/documentation/lead tasks.
const writerRun = L.createRun({ goal: "Serialize writers", acceptanceCriteria: ["Done"] });
L.refreshReady(writerRun);
const pmWriter = L.claimNext(writerRun, { id: "pm-writer", roles: ["project_manager"] });
L.transition(writerRun, pmWriter.task.id, "running", { leaseToken: pmWriter.lease.token });
L.transition(writerRun, pmWriter.task.id, "completed", { evidence: [{ summary: "Plan complete" }] });
L.refreshReady(writerRun);
const extraWriter = {
  ...writerRun.tasks.find((t) => t.role === "implementation"),
  id: writerRun.id + ":task:implementation-extra",
  title: "Second implementation writer",
  dependencies: [],
  status: "ready",
  attempts: 0,
  lease: null,
  ordinal: 99,
};
writerRun.tasks.push(extraWriter);
const writerOne = L.claimNext(writerRun, { id: "writer-one", roles: ["implementation"] });
assert(writerOne);
assert.equal(L.hasActiveWriter(writerRun), true);
assert.equal(L.claimNext(writerRun, { id: "writer-two", roles: ["implementation"] }), null,
  "a second write-capable task must wait while a writer holds the shared workspace");
assert(L.claimNext(writerRun, { id: "parallel-reader", roles: ["ideas_insights"] }),
  "read-only work can still proceed alongside the active writer");

// Manual retry is explicit, bounded by dependency state, and resets only the
// selected task's exhausted attempt budget.
const retryRun = L.createRun({ goal: "Retry blocked task", acceptanceCriteria: ["Done"] });
L.refreshReady(retryRun);
const retryPm = L.claimNext(retryRun, { id: "retry-pm", roles: ["project_manager"] });
L.transition(retryRun, retryPm.task.id, "running", { leaseToken: retryPm.lease.token });
L.transition(retryRun, retryPm.task.id, "completed", { evidence: [{ summary: "Plan complete" }] });
L.refreshReady(retryRun);
const retryImpl = retryRun.tasks.find((t) => t.role === "implementation");
L.transition(retryRun, retryImpl.id, "blocked", { reason: "Exhausted attempts" });
retryImpl.attempts = 3;
L.retryTask(retryRun, retryImpl.id, "User requested retry");
assert.equal(retryImpl.status, "ready");
assert.equal(retryImpl.attempts, 0);
assert.equal(retryRun.state, "running");
assert.equal(retryImpl.blockReason, "");

const dependencyRetry = L.createRun({ goal: "Do not retry unresolved dependency", acceptanceCriteria: ["Done"] });
L.refreshReady(dependencyRetry);
const blockedImpl = dependencyRetry.tasks.find((t) => t.role === "implementation");
L.transition(dependencyRetry, blockedImpl.id, "blocked", { reason: "Dependency incomplete" });
assert.throws(() => L.retryTask(dependencyRetry, blockedImpl.id), /dependencies before retrying/);

const cyclic = L.createRun({ goal: "Cycle", acceptanceCriteria: ["Done"] });
cyclic.tasks[0].dependencies.push(cyclic.tasks[1].id);
cyclic.tasks[1].dependencies.push(cyclic.tasks[0].id);
assert.throws(() => L.validate(cyclic), /cycle/);

// Priorities are clamped and used only among dependency-ready tasks.
const prioritized = L.createRun({ goal: "Prioritize discovery", acceptanceCriteria: ["Evidence exists"], taskPriorities: { ideas_insights: 1000, implementation: -1000 } });
L.refreshReady(prioritized);
const pm2 = L.claimNext(prioritized, { id: "priority-pm", roles: ["project_manager"] });
L.transition(prioritized, pm2.task.id, "running", { leaseToken: pm2.lease.token });
L.transition(prioritized, pm2.task.id, "completed", { evidence: [{ summary: "Plan recorded", source: "test" }] });
const priorityClaim = L.claimNext(prioritized, { id: "priority-worker" });
assert.equal(priorityClaim.task.role, "ideas_insights", "highest priority wins among ready tasks");
assert.equal(priorityClaim.task.priority, 100, "priority is clamped to 100");

// Exercise the complete dependency graph through QA evidence and finalization.
const lifecycle = L.createRun({
  goal: "Exercise full Autopilot lifecycle",
  acceptanceCriteria: ["Feature is implemented", "Tests pass"],
});
const roleOrder = ["project_manager", "implementation", "qa", "code_review", "security", "documentation", "ideas_insights", "lead"];
for (const role of roleOrder) {
  L.refreshReady(lifecycle);
  const claim = L.claimNext(lifecycle, { id: "e2e-" + role, roles: [role] });
  assert(claim, "Expected ready task for " + role);
  L.transition(lifecycle, claim.task.id, "running", { leaseToken: claim.lease.token });
  const evidence = [{ summary: role + " completed with observed evidence", source: "integration-test" }];
  const payload = { evidence };
  if (role === "qa") {
    payload.evidence = lifecycle.acceptanceCriteria.map((criterion) => ({
      summary: "PASS: " + criterion, source: "test output",
      tool: "workspace_run_task", output: "PASS: " + criterion,
    }));
    payload.criteriaEvidence = lifecycle.acceptanceCriteria.map((criterion) => ({
      criterion, status: "VERIFIED", tool: "workspace_run_task", excerpt: criterion,
    }));
  }
  L.transition(lifecycle, claim.task.id, "completed", payload);
}
assert.equal(L.finalize(lifecycle), "completed", "full evidence-backed task graph should complete");

// Stop while a worker holds a lease must cancel the active task and revoke its lease.
const activeStop = L.createRun({ goal: "Stop an active run", acceptanceCriteria: ["No work continues after stop"] });
L.refreshReady(activeStop);
const activeClaim = L.claimNext(activeStop, { id: "active-stop-worker", roles: ["project_manager"] });
L.transition(activeStop, activeClaim.task.id, "running", { leaseToken: activeClaim.lease.token });
L.cancelRun(activeStop, "Stop button regression");
assert.equal(activeStop.state, "cancelled");
assert.equal(activeStop.stopRequested, true);
assert.equal(activeClaim.task.status, "cancelled");
assert.equal(activeClaim.task.lease, null);
assert.equal(L.finalize(activeStop), "cancelled");

process.stdout.write("Autopilot task graph and priority tests passed.\n");
