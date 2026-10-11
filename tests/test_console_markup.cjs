const test = require("node:test");
const assert = require("node:assert/strict");
const { renderChatText } = require("../agentwatch/console/static/messages.js");

test("tool text cannot create active HTML, links or images", () => {
  const html = renderChatText(
    "<img src=x onerror=alert(1)>\n<script>bad()</script>\n[click](javascript:bad())",
  );
  assert(!html.includes("<img"));
  assert(!html.includes("<script"));
  assert(!html.includes("<a "));
  assert(html.includes("&lt;img"));
  assert(html.includes("&lt;script&gt;"));
});
test("MCP lists and citations are legible", () => {
  const html = renderChatText(
    "Serveurs :\n\n- **example** : 4 appels\n- **other** : 2 appels\n\nSource : `sections.mcp.tools`.",
  );
  assert(html.includes("<ul><li><strong>example</strong>"));
  assert(html.includes("<code>sections.mcp.tools</code>"));
  assert(html.includes("</ul><p>Source"));
});
test("code keeps its literal content even if a fence is unfinished", () => {
  const html = renderChatText("```html\n<strong>**literal**</strong>");
  assert.equal(
    html,
    "<pre><code>&lt;strong&gt;**literal**&lt;/strong&gt;</code></pre>",
  );
});
