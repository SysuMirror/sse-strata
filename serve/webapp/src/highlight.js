// 极简语法高亮：零依赖，够看就行。
// 做法：一条主正则把「注释 / 字符串 / 数字 / 关键字」切出来，其余按纯文本转义。
// 刻意不追求完整语法分析 —— 编辑器里读代码，颜色对得上 90% 场景就够，出错也不会破坏文本。
import { esc } from "./api.js";

const KW = {
  js: "const let var function return if else for while do switch case break continue new class extends super this import export from default async await try catch finally throw typeof instanceof in of null undefined true false delete void yield static get set",
  ts: "const let var function return if else for while do switch case break continue new class extends super this import export from default async await try catch finally throw typeof instanceof in of null undefined true false delete void yield static get set interface type enum implements declare readonly public private protected namespace as any unknown never string number boolean",
  py: "def class return if elif else for while import from as try except finally raise with pass break continue and or not in is None True False lambda yield global nonlocal assert del async await print",
  sh: "if then else fi for while do done case esac function return export local echo cd source set unset shift trap exit",
  json: "true false null",
  css: "important media supports keyframes font-face import char set",
  html: "html head body div span a p ul ol li table tr td th script style link meta img section header footer nav main article aside button input form label select option textarea",
};

const LINE_COMMENT = { js: "//", ts: "//", json: "", css: "", html: "", sh: "#", py: "#" };

function langOf(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  if (["js", "mjs", "cjs", "jsx"].includes(ext)) return "js";
  if (["ts", "tsx"].includes(ext)) return "ts";
  if (["py"].includes(ext)) return "py";
  if (["sh", "bash"].includes(ext)) return "sh";
  if (["json"].includes(ext)) return "json";
  if (["css"].includes(ext)) return "css";
  if (["html", "htm", "vue", "xml"].includes(ext)) return "html";
  return "";
}

/** 返回可直接 v-html 的高亮 HTML（内部已转义，安全）。 */
export function highlight(code, path) {
  const lang = langOf(path);
  if (!lang) return esc(code);
  const kw = new Set((KW[lang] || "").split(/\s+/).filter(Boolean));
  const lc = LINE_COMMENT[lang] || "";

  // 主正则的顺序很重要：注释和字符串必须先吃掉，否则字符串里的 // 会被当注释
  const parts = [];
  const re = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*|#[^\n]*|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`|\b\d+(?:\.\d+)?\b|[A-Za-z_$][\w$]*)/g;
  let last = 0, m;
  while ((m = re.exec(code))) {
    if (m.index > last) parts.push(esc(code.slice(last, m.index)));
    const tok = m[0];
    let cls = "";
    if (tok.startsWith("/*") || tok.startsWith("//") || (lc === "#" && tok.startsWith("#"))) cls = "c";
    else if (/^["'`]/.test(tok)) cls = "s";
    else if (/^\d/.test(tok)) cls = "n";
    else if (kw.has(tok)) cls = "k";
    parts.push(cls ? `<span class="hl-${cls}">${esc(tok)}</span>` : esc(tok));
    last = m.index + tok.length;
  }
  if (last < code.length) parts.push(esc(code.slice(last)));
  return parts.join("");
}

export { langOf };