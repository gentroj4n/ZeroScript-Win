// Quick Node smoke test for providers/chatgpt.js (run: node test-chatgpt.js).
// Not shipped.
//
// Why this exists: every ChatGPT bug fixed in 1.5.1 was a READING bug, not a
// parsing one - the parser was fine, the text handed to it was wrong. ChatGPT
// renders code blocks with CodeMirror, which emits one element per line and NO
// newline characters at all, so a naive read glued a whole script onto one line
// ("your code block was empty"), and past a few thousand characters CodeMirror
// stops rendering the rest of a long line, so commands ran truncated. Those are
// exactly the regressions this file guards.
//
// The provider is a browser IIFE, so it is evaluated here against a minimal
// stub DOM (below) - just enough of the Node/Element surface that textWithout
// walks. No jsdom, no npm install: the repo has no dependencies and this test
// keeps it that way.
const fs = require("fs");

// ── Stub DOM ────────────────────────────────────────────────────────────────
// el("div", {class, attrs}, children) / txt("…"). Only the members textWithout
// touches are implemented; anything else stays undefined on purpose so a future
// change that needs more DOM fails loudly here instead of silently passing.
function txt(v) {
  return { nodeType: 3, nodeValue: v, childNodes: [] };
}
function el(tag, opts, children) {
  const o = opts || {};
  const classes = (o.class || "").split(/\s+/).filter(Boolean);
  const attrs = o.attrs || {};
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    childNodes: (children || []).map((c) => (typeof c === "string" ? txt(c) : c)),
    classList: { contains: (c) => classes.includes(c) },
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    matches: (sel) => classes.some((c) => sel.split(",").map((s) => s.trim()).includes("." + c)),
  };
  node.querySelector = (sel) => {
    const hit = (n) => {
      if (n.nodeType !== 1) return null;
      if (n.matches(sel)) return n;
      for (const c of n.childNodes) { const r = hit(c); if (r) return r; }
      return null;
    };
    for (const c of node.childNodes) { const r = hit(c); if (r) return r; }
    return null;
  };
  return node;
}

// ── Load the provider against the stub globals ──────────────────────────────
let querySelectorAllHandler = () => [];
global.document = {
  querySelector: () => null,
  querySelectorAll: (selector) => querySelectorAllHandler(selector),
  getElementById: () => null,
  addEventListener: () => {},
  dispatchEvent: () => {},
  documentElement: { classList: { contains: () => false }, setAttribute: () => {} },
  body: null,
};
global.window = { location: { pathname: "/" }, addEventListener: () => {} };
global.location = global.window.location;
global.CustomEvent = class { constructor(t) { this.type = t; } };
global.MutationObserver = class { observe() {} disconnect() {} };
global.getComputedStyle = () => ({});
const P = new Function(
  fs.readFileSync(__dirname + "/providers/chatgpt.js", "utf8") + "; return ZSProvider;"
)();
const Parse = new Function(
  fs.readFileSync(__dirname + "/core/parser.js", "utf8") + "; return ZSParse;"
)();

const ok = (name, cond) => { console.log((cond ? "PASS" : "FAIL") + "  " + name); if (!cond) process.exitCode = 1; };

// ── Current ChatGPT composer and turn markup (October 2026) ────────────────
// The signed-in Chat layout no longer always exposes data-message-author-role
// or the old composer wrapper. These tests guard the new semantic attributes.
const modernUser = {
  getAttribute: (name) => name === "data-content-search-unit-key" ? "turn-abc:user" : null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
const modernAssistant = {
  getAttribute: (name) => name === "data-content-search-unit-key" ? "turn-abc:assistant" : null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
ok("new user content unit is classified as a user turn", P.isUserItem(modernUser));
ok("new assistant content unit is classified as an assistant turn", P.isAssistantItem(modernAssistant));
ok("new assistant unit supplies a stable turn identity", P.itemKey(modernAssistant) === "turn-abc:assistant");
ok("new assistant unit is a readable reply fallback", P.replyNodes(modernAssistant)[0] === modernAssistant);

// October ChatGPT may render a code block beside (rather than inside) its
// .markdown subtree. The prose node alone is not a complete assistant reply.
const siblingMarkdown = el("div", { class: "markdown" }, ["One moment."]);
const siblingCommand = el("pre", {}, ['{"command":"list_tools","params":{}}']);
const splitReply = {
  getAttribute: (name) => name === "data-message-author-role" ? "assistant" : null,
  querySelectorAll: (selector) => selector === ".markdown" ? [siblingMarkdown] :
    selector === "pre" ? [siblingCommand] : [],
};
ok("orphaned fenced command outside markdown is included in reply nodes",
  P.replyNodes(splitReply).includes(siblingCommand));
ok("orphaned fenced command text reaches the parser",
  P.itemText(splitReply).includes('"command":"list_tools"'));
ok("orphaned fenced list_tools call parses as a tool dispatch",
  Parse.parseToolCalls(P.itemText(splitReply)).some((call) =>
    call.tool === "list_tools" && Object.keys(call.arguments).length === 0));

// Some ChatGPT versions put the code block beside the selected role node,
// inside a common conversation-turn wrapper. Verify the provider climbs only
// to that turn and still returns the command text to the parser.
const turnMarkdown = el("div", { class: "markdown" }, ["Starting Up"]);
const turnCommand = el("pre", {}, ['{"command":"list_tools","params":{}}']);
const turnWrapper = {
  querySelectorAll: (selector) => selector === ".markdown" ? [turnMarkdown] :
    selector === "pre" ? [turnCommand] : [],
};
const turnAssistant = {
  getAttribute: (name) => name === "data-message-author-role" ? "assistant" : null,
  querySelectorAll: (selector) => selector === ".markdown" ? [turnMarkdown] :
    selector === "pre" ? [] : [],
  closest: (selector) => selector === "[data-testid^='conversation-turn-']" ? turnWrapper : null,
};
ok("sibling command is recovered from the same conversation turn",
  P.replyNodes(turnAssistant).includes(turnCommand));
ok("sibling command from turn wrapper parses as list_tools",
  Parse.parseToolCalls(P.itemText(turnAssistant)).some((call) => call.tool === "list_tools"));
const stopIconPath = "M4.5 5.75C4.5 5.05964 5.05964 4.5 5.75 4.5H14.25C14.9404 4.5 15.5 5.05964 15.5 5.75V14.25C15.5 14.9404 14.9404 15.5 14.25 15.5H5.75C5.05964 15.5 4.5 14.9404 4.5 14.25V5.75Z";
const modernStopButton = {
  getAttribute: () => null,
  matches: (selector) => selector.includes(stopIconPath),
};
const modernSendButton = {
  getAttribute: (name) => name === "data-testid" ? "send-button" : null,
  matches: () => false,
};
ok("new icon-based Stop button is recognised", P.isStopBtn(modernStopButton));
ok("new-layout Send button is not mistaken for Stop", !P.isStopBtn(modernSendButton));
querySelectorAllHandler = (selector) => {
  if (selector.includes("data-message-author-role")) return [];
  if (selector.includes("data-content-search-unit-key")) return [modernUser, modernAssistant];
  return [];
};
const modernItems = P.allItems();
ok("new content-search units are enumerated as turns", modernItems.length === 2 &&
  modernItems[0] === modernUser && modernItems[1] === modernAssistant);
const modernComposerForm = {};
const modernComposerEditor = {
  tagName: "DIV",
  id: "",
  offsetParent: {},
  textContent: "",
  getClientRects: () => [],
  getAttribute: () => null,
  setAttribute: () => {},
  removeAttribute: () => {},
  closest: (selector) => {
    if (selector === "form[data-chatgpt-composer]" || selector === "form") return modernComposerForm;
    return null;
  },
};
querySelectorAllHandler = (selector) => {
  if (selector.includes("data-composer-markdown") || selector.includes('data-testid="prompt-textarea"') || selector.includes("#prompt-textarea")) {
    return [modernComposerEditor];
  }
  return [];
};
ok("new composer contenteditable is found", P.getEditor() === modernComposerEditor);
ok("new composer uses anchored fallback when no safe grid surface exists", P.barMount() === null);
ok("new composer anchor targets its React form", P.barAnchor() === modernComposerForm);
querySelectorAllHandler = () => [];

// ── The CodeMirror line collapse (the 1.5.1 headline bug) ───────────────────
// A code block is <div class="cm-content"> with one <div class="cm-line"> per
// source line and no newline text nodes anywhere. Reading it with textContent
// returns `###LUA###return 1+1###END_LUA###` - one line, unparseable. Every
// cm-line must terminate.
const cmBlock = (lines, attrs) =>
  el("div", { class: "cm-content", attrs: attrs || {} },
     lines.map((l) => el("div", { class: "cm-line" }, l === "" ? [] : [l])));

const collapsed = P.textWithout(cmBlock(["###LUA###", "return 1+1", "###END_LUA###"]));
ok("cm-lines are newline-terminated", collapsed.includes("###LUA###\nreturn 1+1\n###END_LUA###"));

// A blank source line is an EMPTY cm-line. Swallowing it shifts every Luau
// error line number reported afterwards, so it must survive as "\n".
const blanks = P.textWithout(cmBlock(["local a = 1", "", "return a"]));
ok("empty cm-line survives as a blank line", /local a = 1\n\nreturn a/.test(blanks));

// ── The MAIN-world tap wins over the rendered lines ─────────────────────────
// Past ~2000-4000 chars CodeMirror renders only PART of a long line, so the
// visible lines are a truncated copy. providers/chatgpt-cm.js publishes the
// editor's true document on data-zs-cm; when present it must be used verbatim
// and the (short) rendered subtree ignored - this is what made a 21k-character
// command stop executing cut off.
const full = '{"command":"multi_edit","params":{"edits":[{"old_string":"a","new_string":"' + "x".repeat(5000) + '"}]}}';
const tapped = P.textWithout(cmBlock(['{"command":"multi_edit","params":{"edits":[{"old_str'], { "data-zs-cm": full }));
ok("data-zs-cm tap is preferred over rendered lines", tapped.includes(full));
ok("tapped block parses as JSON", (() => {
  const s = tapped.slice(tapped.indexOf("{"), tapped.lastIndexOf("}") + 1);
  try { return JSON.parse(s).params.edits[0].new_string.length === 5000; } catch { return false; }
})());
// An EMPTY tap value is still a real document (an empty editor), not a missing
// attribute: getAttribute must be checked against null, never falsiness.
const emptyTap = P.textWithout(cmBlock(["stale rendered text"], { "data-zs-cm": "" }));
ok("empty tap value is honoured, not treated as absent", !emptyTap.includes("stale"));

// ── Ordinary prose is untouched ─────────────────────────────────────────────
// Block elements break lines; inline text must not gain stray newlines, or the
// core's marker matching sees a shape the model never wrote.
const prose = P.textWithout(
  el("div", {}, [
    el("p", {}, ["Running the command now:"]),
    el("p", {}, ["Done ", el("strong", {}, ["with"]), " no gaps."]),
  ])
);
ok("blocks separate, inline text stays joined", /Running the command now:\nDone with no gaps\./.test(prose));
// (A block closes with a trailing newline of its own - hence the optional \n.)
ok("a <br> is a newline", /^a\nb\n?$/.test(P.textWithout(el("p", {}, ["a", el("br", {}), "b"]))));

// ── excludeSel ──────────────────────────────────────────────────────────────
// The core passes ".zs-chip" so its own injected chip never counts as model
// output (a chip echoing a tool name would otherwise re-trigger the call).
const withChip = P.textWithout(
  el("div", {}, [el("p", {}, ["real reply"]), el("div", { class: "zs-chip" }, ["execute_luau · 2s"])]),
  ".zs-chip"
);
ok("excluded subtree is skipped", withChip.includes("real reply") && !withChip.includes("execute_luau"));
