// SPDX-License-Identifier: GPL-3.0-or-later
// Pure task-graph and evidence ledger for the Autopilot coordinator.
// Deliberately independent of Chrome APIs so transitions are unit-testable.
(function (root) {
  "use strict";
  const ROLES = ["project_manager", "implementation", "qa", "code_review", "security", "documentation", "ideas_insights", "lead"];
  const TERMINAL = new Set(["completed", "blocked", "needs_approval", "failed", "cancelled"]);
  const TASK_STATES = new Set(["queued", "ready", "leased", "running", "review", "completed", "blocked", "needs_approval", "failed", "cancelled"]);
  const RUN_STATES = new Set(["planning", "running", "verifying", ...TERMINAL]);
  const MAX_TASKS = 40;
  const MAX_EVENTS = 300;
  const MAX_RETRIES = 2;
  const DEFAULT_PRIORITY = { project_manager: 100, implementation: 90, qa: 80, security: 75, code_review: 70, documentation: 50, ideas_insights: 40, lead: 10 };

  function id(prefix) {
    const random = Math.random().toString(36).slice(2, 10);
    return prefix + "-" + Date.now().toString(36) + "-" + random;
  }
  function now() { return new Date().toISOString(); }
  function cleanText(value, max = 12000) {
    return String(value == null ? "" : value).trim().slice(0, max);
  }
  function assert(condition, message) { if (!condition) throw new Error(message); }
  function createRun(input = {}) {
    const goal = cleanText(input.goal, 8000);
    assert(goal, "A non-empty goal is required.");
    const criteria = Array.isArray(input.acceptanceCriteria)
      ? input.acceptanceCriteria.map((x) => cleanText(x, 500)).filter(Boolean).slice(0, 20)
      : [];
    assert(criteria.length > 0, "At least one explicit acceptance criterion is required.");
    const runId = id("run");
    const tasks = [
      { role: "project_manager", title: "Define plan and dependencies", dependencies: [], writeScope: [], acceptanceCriteria: ["Plan decomposes the goal into verifiable deliverables"] },
      { role: "implementation", title: "Implement scoped changes", dependencies: ["project_manager"], writeScope: [], acceptanceCriteria: ["Changes address the agreed acceptance criteria"] },
      { role: "qa", title: "Independently verify acceptance criteria", dependencies: ["implementation"], writeScope: [], acceptanceCriteria: ["Each criterion has a test or explicit blocker"] },
      { role: "code_review", title: "Review implementation diff", dependencies: ["implementation"], writeScope: [], acceptanceCriteria: ["Review findings are recorded and actionable issues resolved or escalated"] },
      { role: "security", title: "Review security and action boundaries", dependencies: ["implementation"], writeScope: [], acceptanceCriteria: ["Security findings are recorded; approval-gated actions are not executed"] },
      { role: "documentation", title: "Align documentation with verified behavior", dependencies: ["qa", "code_review", "security"], writeScope: [], acceptanceCriteria: ["Documentation claims match implementation and evidence"] },
      { role: "ideas_insights", title: "Identify and rank grounded opportunities", dependencies: ["project_manager"], writeScope: [], acceptanceCriteria: ["Ranked opportunities include evidence, impact, effort, risk, and disposition"] },
      { role: "lead", title: "Integrate findings and close out", dependencies: ["qa", "code_review", "security", "documentation", "ideas_insights"], writeScope: [], acceptanceCriteria: ["All required criteria are verified or run is explicitly blocked"] },
    ].map((task, index) => ({
      id: runId + ":task:" + task.role,
      runId, role: task.role, title: task.title, dependencies: task.dependencies.map((d) => runId + ":task:" + d),
      status: "queued", attempts: 0, maxAttempts: MAX_RETRIES, writeScope: task.writeScope,
      acceptanceCriteria: task.acceptanceCriteria,
      priority: Number.isFinite(Number(input.taskPriorities && input.taskPriorities[task.role]))
        ? Math.max(-100, Math.min(100, Number(input.taskPriorities[task.role])))
        : DEFAULT_PRIORITY[task.role],
      evidence: [], findings: [], lease: null,
      createdAt: now(), updatedAt: now(), ordinal: index,
    }));
    return {
      schemaVersion: 1, id: runId, goal, workspace: cleanText(input.workspace, 2000),
      acceptanceCriteria: criteria, state: "planning", stopRequested: false,
      tasks, events: [{ id: id("evt"), type: "run.created", at: now(), detail: "Run and dependency graph created." }],
      budget: { maxTasks: MAX_TASKS, maxRetriesPerTask: MAX_RETRIES, maxRunMs: Math.max(60000, Math.min(Number(input.maxRunMs) || 1800000, 7200000)), startedAt: null, deadlineAt: null },
      createdAt: now(), updatedAt: now(), result: null,
    };
  }
  function event(run, type, detail, taskId) {
    run.events.push({ id: id("evt"), type, taskId: taskId || null, detail: cleanText(detail, 1000), at: now() });
    if (run.events.length > MAX_EVENTS) run.events.splice(0, run.events.length - MAX_EVENTS);
    run.updatedAt = now();
  }
  function validate(run) {
    assert(run && run.schemaVersion === 1 && typeof run.id === "string", "Invalid Autopilot run record.");
    assert(RUN_STATES.has(run.state), "Invalid run state.");
    assert(Array.isArray(run.tasks) && run.tasks.length <= MAX_TASKS, "Invalid task graph size.");
    const ids = new Set(run.tasks.map((t) => t.id));
    for (const task of run.tasks) {
      assert(TASK_STATES.has(task.status), "Invalid task state.");
      assert(ROLES.includes(task.role), "Invalid task role.");
      assert(task.dependencies.every((dep) => ids.has(dep) && dep !== task.id), "Task dependency is missing or self-referential.");
    }
    // Detect dependency cycles before scheduling.
    const visiting = new Set(), visited = new Set();
    function visit(task) {
      if (visiting.has(task.id)) throw new Error("Task graph contains a dependency cycle.");
      if (visited.has(task.id)) return;
      visiting.add(task.id);
      for (const dep of task.dependencies) visit(run.tasks.find((t) => t.id === dep));
      visiting.delete(task.id); visited.add(task.id);
    }
    run.tasks.forEach(visit);
    return run;
  }
  function transition(run, taskId, next, payload = {}) {
    validate(run);
    const task = run.tasks.find((t) => t.id === taskId);
    assert(task, "Unknown task: " + taskId);
    assert(TASK_STATES.has(next), "Unknown task state.");
    const allowed = {
      queued: ["ready", "cancelled", "blocked"], ready: ["leased", "cancelled", "blocked"],
      leased: ["running", "ready", "failed", "cancelled"], running: ["review", "completed", "blocked", "needs_approval", "failed", "cancelled", "ready"],
      review: ["completed", "ready", "blocked", "needs_approval", "failed", "cancelled"],
      completed: [], blocked: ["ready"], needs_approval: ["ready", "cancelled"], failed: ["ready", "cancelled"], cancelled: [],
    };
    assert(allowed[task.status].includes(next), "Invalid task transition: " + task.status + " -> " + next);
    if (next === "completed") {
      assert(Array.isArray(payload.evidence) && payload.evidence.some((e) => cleanText(e && e.summary)), "A completed task requires concrete evidence.");
      if (task.role === "qa") {
        const criteriaEvidence = Array.isArray(payload.criteriaEvidence) ? payload.criteriaEvidence : [];
        assert(run.acceptanceCriteria.length > 0, "QA cannot complete without run acceptance criteria.");
        for (const criterion of run.acceptanceCriteria) {
          const item = criteriaEvidence.find((entry) => entry && entry.criterion === criterion && entry.status === "VERIFIED");
          assert(item, "QA requires structured evidence for criterion: " + criterion);
          const excerpt = cleanText(item.excerpt, 500);
          assert(excerpt.length >= 8, "QA evidence excerpt is too short for criterion: " + criterion);
          const source = payload.evidence.find((entry) =>
            entry && cleanText(entry.tool, 200) === cleanText(item.tool, 200) &&
            cleanText(entry.output, 5000).toLowerCase().includes(excerpt.toLowerCase())
          );
          assert(source, "QA evidence excerpt must match an observed successful tool output for criterion: " + criterion);
        }
      }
    }
    if (next === "leased" || next === "running") {
      if (next === "leased") {
        assert(cleanText(payload.workerId, 200), "A worker ID is required for a lease.");
        assert(task.attempts < task.maxAttempts + 1, "Task retry budget exhausted.");
        task.attempts++;
        task.lease = { workerId: cleanText(payload.workerId, 200), provider: cleanText(payload.provider, 100), tabId: Number.isInteger(payload.tabId) ? payload.tabId : null, token: id("lease"), expiresAt: new Date(Date.now() + Math.max(30000, Math.min(Number(payload.leaseMs) || 180000, 600000))).toISOString() };
      }
      if (next === "running") {
        assert(task.lease && task.lease.token === payload.leaseToken, "Valid worker lease token required.");
      }
    }
    task.status = next;
    task.updatedAt = now();
    if (next === "completed") {
      // Raw tool output is used for validation above but is not retained in the
      // durable ledger. Keeping only summaries and the exact QA excerpts prevents
      // large tool payloads from exhausting chrome.storage.local quota.
      task.evidence = payload.evidence.map((e) => ({
        summary: cleanText(e.summary, 1200),
        source: cleanText(e.source, 500),
        tool: cleanText(e.tool, 200),
        verifiedAt: now(),
      })).slice(0, 30);
      task.findings = Array.isArray(payload.findings) ? payload.findings.map((x) => cleanText(x, 1000)).filter(Boolean).slice(0, 30) : [];
      task.criteriaEvidence = task.role === "qa" ? run.acceptanceCriteria.map((criterion) => {
        const entry = payload.criteriaEvidence.find((candidate) => candidate && candidate.criterion === criterion && candidate.status === "VERIFIED");
        return {
          criterion,
          status: "VERIFIED",
          tool: cleanText(entry.tool, 200),
          excerpt: cleanText(entry.excerpt, 500),
        };
      }) : [];
      task.criteriaVerified = task.role === "qa" ? task.criteriaEvidence.map((entry) => entry.criterion) : [];
      task.lease = null;
    } else if (["blocked", "needs_approval", "failed", "cancelled", "ready"].includes(next)) {
      if (payload.reason) task.blockReason = cleanText(payload.reason, 1000);
      if (next !== "running") task.lease = null;
    }
    event(run, "task." + next, payload.reason || payload.workerId || "Task state changed.", taskId);
    return task;
  }
  function refreshReady(run) {
    validate(run);
    for (const task of run.tasks) {
      if (task.status !== "queued") continue;
      const deps = task.dependencies.map((id) => run.tasks.find((t) => t.id === id));
      if (deps.some((d) => ["blocked", "failed", "cancelled", "needs_approval"].includes(d.status))) {
        transition(run, task.id, "blocked", { reason: "A dependency did not complete." });
      } else if (deps.every((d) => d.status === "completed")) {
        transition(run, task.id, "ready", { reason: "Dependencies satisfied." });
      }
    }
    if (run.state === "planning") {
      run.state = "running";
      run.budget.startedAt = run.budget.startedAt || now();
      run.budget.deadlineAt = run.budget.deadlineAt || new Date(Date.now() + run.budget.maxRunMs).toISOString();
    }
    run.updatedAt = now();
    return run.tasks.filter((t) => t.status === "ready");
  }
  function isWriteCapableRole(role) {
    return role === "implementation" || role === "documentation" || role === "lead";
  }
  function hasActiveWriter(run) {
    return run.tasks.some((task) =>
      isWriteCapableRole(task.role) && ["leased", "running", "review"].includes(task.status)
    );
  }
  function claimNext(run, worker) {
    refreshReady(run);
    assert(!run.stopRequested && !TERMINAL.has(run.state), "Run is not accepting work.");
    assert(Date.now() < Date.parse(run.budget.deadlineAt), "Run time budget exhausted.");
    // All workers currently operate on the same selected workspace. Until
    // per-task worktrees are available, serialize every write-capable role.
    // Read-only analysis/QA/review workers may still run concurrently.
    const writerActive = hasActiveWriter(run);
    const task = run.tasks.filter((t) =>
      t.status === "ready" &&
      (!worker.roles || worker.roles.includes(t.role)) &&
      !(writerActive && isWriteCapableRole(t.role))
    ).sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0) || a.ordinal - b.ordinal)[0];
    if (!task) return null;
    transition(run, task.id, "leased", { workerId: worker.id, provider: worker.provider, tabId: worker.tabId, leaseMs: worker.leaseMs });
    return { task, lease: { ...task.lease } };
  }
  function renewLease(run, taskId, leaseToken, leaseMs = 600000) {
    validate(run);
    const task = run.tasks.find((t) => t.id === taskId);
    assert(task && ["leased", "running"].includes(task.status), "Task is not actively leased.");
    assert(task.lease && task.lease.token === leaseToken, "Worker lease token is invalid.");
    task.lease.expiresAt = new Date(Date.now() + Math.max(30000, Math.min(Number(leaseMs) || 600000, 600000))).toISOString();
    task.updatedAt = now();
    event(run, "task.lease_renewed", "Worker lease renewed.", taskId);
    return task.lease;
  }
  function recoverExpiredLeases(run) {
    const recovered = [];
    for (const task of run.tasks) {
      if (task.status !== "leased" && task.status !== "running") continue;
      if (task.lease && Date.parse(task.lease.expiresAt) > Date.now()) continue;
      const target = task.attempts <= task.maxAttempts ? "ready" : "failed";
      transition(run, task.id, target, { reason: "Worker lease expired; task recovered after interruption." });
      recovered.push(task.id);
    }
    if (recovered.length) event(run, "run.recovered", recovered.length + " expired worker lease(s) recovered.");
    return recovered;
  }
  function cancelRun(run, reason = "Cancelled by user.") {
    run.stopRequested = true;
    for (const task of run.tasks) {
      if (["queued", "ready", "leased", "running", "review"].includes(task.status)) {
        // Cancellation is an orchestrator action; bypass worker-owned transition checks.
        task.status = "cancelled"; task.lease = null; task.updatedAt = now();
      }
    }
    run.state = "cancelled";
    event(run, "run.cancelled", reason);
    return run;
  }
  function retryTask(run, taskId, reason = "Manual retry requested.") {
    validate(run);
    assert(!["completed", "cancelled", "needs_approval"].includes(run.state), "This run state cannot be retried: " + run.state);
    const task = run.tasks.find((entry) => entry.id === taskId);
    assert(task, "Unknown task: " + taskId);
    assert(["blocked", "failed"].includes(task.status), "Only blocked or failed tasks can be retried.");
    const dependencies = task.dependencies.map((dependencyId) => run.tasks.find((entry) => entry.id === dependencyId));
    assert(dependencies.every((dependency) => dependency && dependency.status === "completed"),
      "Resolve and complete this task's dependencies before retrying it.");
    task.attempts = 0;
    run.state = "running";
    run.stopRequested = false;
    transition(run, task.id, "ready", { reason: cleanText(reason, 1000) || "Manual retry requested." });
    task.blockReason = "";
    refreshReady(run);
    event(run, "task.manual_retry", "Task retry budget reset by explicit user action.", task.id);
    return run;
  }

  function finalize(run) {
    validate(run);
    if (run.stopRequested) { run.state = "cancelled"; return run.state; }
    const unfinished = run.tasks.some((t) => ["queued", "ready", "leased", "running", "review"].includes(t.status));
    if (unfinished) {
      run.state = "verifying";
    } else if (run.tasks.some((t) => t.status === "needs_approval")) {
      run.state = "needs_approval";
    } else if (run.tasks.some((t) => ["blocked", "failed", "cancelled"].includes(t.status))) {
      run.state = "blocked";
    } else if (run.tasks.every((t) => t.status === "completed")) {
      const qa = run.tasks.find((t) => t.role === "qa");
      const lead = run.tasks.find((t) => t.role === "lead");
      if (!qa || !qa.criteriaVerified || !run.acceptanceCriteria.every((criterion) => qa.criteriaVerified.includes(criterion))) run.state = "blocked";
      else if (!lead || !lead.evidence.length) run.state = "blocked";
      else run.state = "completed";
    } else run.state = "verifying";
    run.updatedAt = now();
    event(run, "run." + run.state, "Run state evaluated.");
    return run.state;
  }
  const api = { ROLES, TERMINAL, DEFAULT_PRIORITY, createRun, validate, transition, refreshReady, claimNext, renewLease, recoverExpiredLeases, cancelRun, retryTask, finalize, isWriteCapableRole, hasActiveWriter };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ZSAutopilotLedger = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
