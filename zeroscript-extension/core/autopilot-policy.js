// SPDX-License-Identifier: GPL-3.0-or-later
// Enforced capability policy for independent Autopilot workers.
// This is a defense-in-depth dispatch gate, not a replacement for OS sandboxing.
(function (root) {
  "use strict";

  const READ_ONLY_ROLES = new Set([
    "project_manager", "qa", "code_review", "security", "ideas_insights",
  ]);
  const NEVER_AUTOMATE = /(?:^|[_/.-])(?:delete|remove|destroy|purge|wipe|format|reset|set_root|open_recent|move_path|git_commit|git_push|git_stage_file|git_unstage_file|publish|deploy|release|send_email|send_message|purchase|checkout|payment|credential|secret_rotation|change_password|install|uninstall)(?:$|[_/.-])/i;
  const READ_VERB = /^(?:get|list|read|inspect|search|find|query|describe|show|status|info|outline|tree|log|diff|branches|symbols|capture|screenshot|screen_capture|health|probe|count|stat|head|grep|locate|lookup|history|explain|analyze|validate|check)$/i;
  const MUTATING_TOKEN = /(?:^|[_/.-])(?:write|create|set|update|patch|edit|fix|repair|run|execute|apply|insert|store|delete|remove|destroy|deploy|publish|commit|push|move|send|purchase|install|uninstall|format|reset)(?:$|[_/.-])/i;
  const READ_NAME = /^(?:workspace_(?:info|recent_roots|project_info|tree|file_outline|list_files|read_file|search|find_files|symbols|git_status|git_diff|git_branches|git_log)|list_tools|list_commands|list_mcp_servers|search_game_tree|inspect_instance|get_studio_state|script_read|screen_capture|screenshot|workspace_preview_image)$/i;
  const DOC_PATH = /(?:^|[/\\])(?:docs?[/\\]|\.github[/\\].*\.md$)|(?:^|[/\\])(?:readme|changelog|contributing|license|security|code_of_conduct)(?:\.[a-z0-9_-]+)?$/i;
  const EDIT_TOOLS = new Set([
    "workspace_write_file", "workspace_apply_patch", "workspace_multi_edit",
    "workspace_replace_in_files", "workspace_create_file", "multi_edit",
  ]);

  function bareName(tool) {
    const value = String(tool || "");
    return value.includes("/") ? value.slice(value.lastIndexOf("/") + 1) : value;
  }
  function readLike(name, category) {
    const bare = bareName(name);
    if (MUTATING_TOKEN.test(bare)) return false;
    if (READ_NAME.test(bare)) return true;
    if (category === "read" || category === "screen") return true;
    const first = bare.split(/[_-]/)[0];
    return READ_VERB.test(first);
  }
  function pathValues(args) {
    if (!args || typeof args !== "object") return [];
    const out = [];
    for (const key of ["path", "file", "filename", "target", "destination", "directory"]) {
      if (typeof args[key] === "string") out.push(args[key]);
    }
    for (const key of ["files", "paths", "edits", "changes"]) {
      if (Array.isArray(args[key])) {
        for (const item of args[key]) {
          if (typeof item === "string") out.push(item);
          else if (item && typeof item === "object") {
            for (const field of ["path", "file", "filename"]) if (typeof item[field] === "string") out.push(item[field]);
          }
        }
      }
    }
    return out;
  }
  function decide(input = {}) {
    const role = String(input.role || "");
    const name = bareName(input.tool);
    const category = String(input.category || "tool");
    const args = input.args || {};
    if (!name) return { allowed: false, reason: "Tool name is missing." };
    if (NEVER_AUTOMATE.test(name)) {
      return { allowed: false, reason: "This operation is destructive, externally consequential, or approval-gated; Autopilot cannot execute it automatically." };
    }
    if (READ_ONLY_ROLES.has(role)) {
      if (readLike(name, category) && !/^(?:execute_|run_|write_|create_|set_|update_|patch_|edit_|multi_edit|insert_|store_)/i.test(name)) {
        return { allowed: true, capability: "read" };
      }
      if (role === "qa" && name === "workspace_run_task" && /^(?:test|lint|check|verify|build)$/i.test(String(args.task || args.name || ""))) {
        return { allowed: true, capability: "verification" };
      }
      return { allowed: false, reason: "Role '" + role + "' is read-only. This tool is not classified as a safe read/inspection operation." };
    }
    if (role === "documentation") {
      if (readLike(name, category)) return { allowed: true, capability: "read" };
      if (!EDIT_TOOLS.has(name)) return { allowed: false, reason: "Documentation workers may only use the approved file-edit tools." };
      const paths = pathValues(args);
      const safeRelativePath = (value) => {
        const path = String(value).replace(/\\/g, "/");
        if (!path || path.startsWith("/") || /^[a-z]:/i.test(path)) return false;
        const parts = path.split("/");
        if (parts.some((part) => part === ".." || part === "." || !part)) return false;
        return DOC_PATH.test(path);
      };
      if (!paths.length || paths.some((path) => !safeRelativePath(path))) {
        return { allowed: false, reason: "Documentation workers may edit only safe workspace-relative README/changelog/contributing/license/security/code-of-conduct files or files under docs/." };
      }
      return { allowed: true, capability: "documentation_edit" };
    }
    if (role === "implementation") {
      if (/^(?:workspace_delete_file|workspace_move_path|workspace_set_root|workspace_git_commit|workspace_git_push|workspace_git_stage_file|workspace_git_unstage_file)$/.test(name)) {
        return { allowed: false, reason: "Autopilot implementation workers cannot delete/move files, change workspace roots, or change Git index/history automatically." };
      }
      return { allowed: true, capability: category === "read" || category === "screen" ? "read" : "write" };
    }
    if (role === "lead") {
      if (/^(?:workspace_delete_file|workspace_move_path|workspace_set_root|workspace_git_commit|workspace_git_push|workspace_git_stage_file|workspace_git_unstage_file)$/.test(name)) {
        return { allowed: false, reason: "Lead workers cannot perform destructive filesystem or Git state changes automatically." };
      }
      return { allowed: true, capability: category === "read" || category === "screen" ? "read" : "write" };
    }
    return { allowed: false, reason: "Unknown Autopilot role; no tool capabilities are granted." };
  }

  const api = { READ_ONLY_ROLES: Array.from(READ_ONLY_ROLES), bareName, readLike, pathValues, decide };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ZSAutopilotPolicy = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
