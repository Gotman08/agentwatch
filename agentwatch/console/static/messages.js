"use strict";
// A deliberately small renderer: HTML, links and images remain plain escaped text.
function renderChatText(value) {
  const escape = (text) =>
    String(text).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const inline = (text) =>
    text
      .split(/(`[^`]+`)/g)
      .map((part) =>
        part.startsWith("`") && part.endsWith("`")
          ? "<code>" + escape(part.slice(1, -1)) + "</code>"
          : escape(part).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"),
      )
      .join("");
  const result = [],
    paragraph = [],
    code = [];
  let list = "",
    fenced = false;
  const flush = () => {
    if (paragraph.length) {
      result.push("<p>" + inline(paragraph.join("\n")) + "</p>");
      paragraph.length = 0;
    }
    if (list) {
      result.push("</" + list + ">");
      list = "";
    }
  };
  for (const line of String(value || "").split("\n")) {
    if (/^\s*```/.test(line)) {
      if (fenced) {
        result.push("<pre><code>" + escape(code.join("\n")) + "</code></pre>");
        code.length = 0;
      } else flush();
      fenced = !fenced;
      continue;
    }
    if (fenced) {
      code.push(line);
      continue;
    }
    const item = line.match(/^\s*(?:[-*]|\d+[.)])\s+(.+)$/);
    if (item) {
      const kind = /^\s*\d/.test(line) ? "ol" : "ul";
      if (list !== kind) {
        flush();
        result.push("<" + kind + ">");
        list = kind;
      }
      result.push("<li>" + inline(item[1]) + "</li>");
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    if (list) flush();
    const heading = line.match(/^#{1,6}\s+(.+)$/);
    if (heading) {
      flush();
      result.push("<h4>" + inline(heading[1]) + "</h4>");
    } else paragraph.push(line);
  }
  if (fenced)
    result.push("<pre><code>" + escape(code.join("\n")) + "</code></pre>");
  flush();
  return result.join("");
}
if (typeof module !== "undefined") module.exports = { renderChatText };
