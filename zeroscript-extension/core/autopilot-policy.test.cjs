// SPDX-License-Identifier: GPL-3.0-or-later
"use strict";
const assert = require("node:assert/strict");
const P = require("./autopilot-policy.js");

function decide(role, tool, args = {}, category = "tool") {
  return P.decide({ role, tool, args, category });
}

for (const role of ["project_manager", "qa", "code_review", "security", "ideas_insights"]) {
  assert.equal(decide(role, "workspace_read_file", { path: "src/app.js" }, "read").allowed, true, role + " can read");
  assert.equal(decide(role, "workspace_write_file", { path: "src/app.js" }, "edit").allowed, false, role + " cannot write");
  assert.equal(decide(role, "execute_luau", {}, "roblox").allowed, false, role + " cannot execute arbitrary code");
}
assert.equal(decide("qa", "workspace_run_task", { task: "test" }, "edit").allowed, true);
assert.equal(decide("qa", "workspace_run_task", { task: "install" }, "edit").allowed, false);
assert.equal(decide("qa", "check_and_fix", {}, "tool").allowed, false, "mixed read/mutate tools must not pass the read-only gate");
assert.equal(decide("documentation", "workspace_write_file", { path: "docs/guide.md" }, "edit").allowed, true);
assert.equal(decide("documentation", "workspace_write_file", { path: "README.md" }, "edit").allowed, true);
assert.equal(decide("documentation", "workspace_write_file", { path: "/tmp/docs/guide.md" }, "edit").allowed, false, "absolute paths must be denied");
assert.equal(decide("documentation", "workspace_write_file", { path: "src/app.js" }, "edit").allowed, false);
assert.equal(decide("documentation", "workspace_multi_edit", { edits: [{ path: "docs/a.md" }, { path: "src/a.js" }] }, "edit").allowed, false);
assert.equal(decide("implementation", "workspace_write_file", { path: "src/app.js" }, "edit").allowed, true);
assert.equal(decide("implementation", "workspace_delete_file", { path: "src/app.js" }, "edit").allowed, false);
assert.equal(decide("implementation", "workspace_git_commit", {}, "edit").allowed, false);
assert.equal(decide("lead", "workspace_move_path", {}, "edit").allowed, false);
assert.equal(decide("lead", "workspace_read_file", {}, "read").allowed, true);
assert.equal(decide("unrecognized-role", "workspace_read_file", {}, "read").allowed, false);
assert.equal(decide("implementation", "publish_release", {}, "tool").allowed, false);
assert.equal(decide("security", "workspace_preview_image", {}, "screen").allowed, true);
assert.equal(decide("implementation", "workspace_git_stage_file", {}, "edit").allowed, false);
assert.equal(decide("implementation", "workspace_git_unstage_file", {}, "edit").allowed, false);
assert.equal(decide("implementation", "workspace_set_root", {}, "tool").allowed, false);
assert.equal(decide("implementation", "workspace_install_package", {}, "tool").allowed, false);
assert.equal(decide("implementation", "workspace_uninstall_package", {}, "tool").allowed, false);
assert.equal(decide("implementation", "workspace_git_reset", {}, "tool").allowed, false);
assert.equal(decide("documentation", "workspace_write_file", { path: "../docs/guide.md" }, "edit").allowed, false);

const fs = require("node:fs");
const path = require("node:path");
const sharedCases = JSON.parse(fs.readFileSync(path.join(__dirname, "../../docs/permission-policy-cases.json"), "utf8"));
for (const name of sharedCases.denied) {
  assert.equal(decide("implementation", name, {}, "tool").allowed, false, "shared denylist must block " + name);
}

process.stdout.write("Autopilot capability policy tests passed.\n");
