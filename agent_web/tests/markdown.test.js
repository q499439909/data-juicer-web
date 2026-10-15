const assert = require("node:assert/strict");
const {renderMarkdown} = require("../static/markdown.js");

const rendered = renderMarkdown("## 能力\n\n**可以做的：**\n\n1. 图片处理\n2. 质量筛选");

assert.match(rendered, /<h2>能力<\/h2>/);
assert.match(rendered, /<strong>可以做的：<\/strong>/);
assert.match(rendered, /<ol>/);
assert.doesNotMatch(rendered, /##|\*\*/);

const unsafe = renderMarkdown('<img src=x onerror="alert(1)">');
assert.doesNotMatch(unsafe, /<img/);
assert.match(unsafe, /&lt;img/);
