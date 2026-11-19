// 解析模型输出的回归测试。纯函数、不用浏览器，秒出结果。
//
// 为什么单独测这个：它面对的是**模型返回的自由文本**，格式没有任何保证。
// 下面每一条样例都是模型真会吐出来的形状（带编号、带引号、带开场白、混英文、空行……），
// 不是编出来凑数的边界。
//
// 用法：node tools/test-parse.mjs
import { parseSuggestions, dedupe, clean } from "../src/text.js";

const FAILS = [];
const ck = (name, ok, detail = "") => {
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (detail ? `  ${detail}` : ""));
  if (!ok) FAILS.push(name);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---- 规范的输出
ck("干净的行直接收下",
   eq(parseSuggestions("解释一下 KV cache 是干什么的\n写一个正则匹配 ISO 日期"), ["解释一下 KV cache 是干什么的", "写一个正则匹配 ISO 日期"]));

// ---- 编号 / 项目符号
ck("去掉 1. / 2、/ 3) / （4） 各种编号",
   eq(parseSuggestions("1. 解释一下 KV cache\n2、写一个正则\n3) 翻译这段话\n（4）找重复记录"),
      ["解释一下 KV cache", "写一个正则", "翻译这段话", "找重复记录"]));
ck("去掉 - / • / · 项目符号",
   eq(parseSuggestions("- 解释 KV cache\n• 写个脚本\n· 翻译一下"), ["解释 KV cache", "写个脚本", "翻译一下"]));

// ---- 模型常见的开场白与结尾
ck("丢掉「当然，以下是…」这类开场白",
   eq(parseSuggestions("当然，以下是 6 个问题：\n1. 解释 KV cache\n希望这些对你有帮助"),
      ["解释 KV cache"]));
ck("丢掉「例如」「注意」开头的行",
   eq(parseSuggestions("例如：怎么写正则\n注意保持简短\n写个脚本统计行数"), ["写个脚本统计行数"]));

// ---- 引号与空白
ck("剥掉成对的中英文引号",
   eq(parseSuggestions("“解释 KV cache”\n『写个脚本』\n\"翻译这段话\""), ["解释 KV cache", "写个脚本", "翻译这段话"]));
ck("压掉多余空白与空行",
   eq(parseSuggestions("  解释   KV    cache   \n\n\n  写个脚本  "), ["解释 KV cache", "写个脚本"]));

// ---- 过滤规则
ck("过短的行丢掉", eq(parseSuggestions("好\n解释 KV cache"), ["解释 KV cache"]));
ck("过长的行丢掉（>42 字）",
   eq(parseSuggestions("解释 KV cache\n" + "这是一个非常非常长的句子".repeat(6)), ["解释 KV cache"]));
ck("不含汉字的行丢掉", eq(parseSuggestions("Explain the KV cache\n解释 KV cache"), ["解释 KV cache"]));

// ---- 去重
ck("重复的问题只留一条",
   eq(parseSuggestions("解释 KV cache\n解释 KV cache\n写个脚本"), ["解释 KV cache", "写个脚本"]));
ck("去重是保序的", eq(dedupe(["b", "a", "b", "c"]), ["b", "a", "c"]));

// ---- 上限
ck("最多返回 max 条", parseSuggestions(Array.from({ length: 20 }, (_, i) => `第${i}个问题是什么`).join("\n"), 5).length === 5);

// ---- 退化输入不该炸
ck("空字符串 → 空数组", eq(parseSuggestions(""), []));
ck("null / undefined → 空数组", eq(parseSuggestions(null), []) && eq(parseSuggestions(undefined), []));
ck("全是噪声 → 空数组", eq(parseSuggestions("1.\n\n- \n“”"), []));
ck("clean 处理 null", clean(null) === "" && clean("  a  b ") === "a b");

// ---- 真实形状的一次完整样例
const REAL = `好的，下面是 6 个问题示例：

1. 解释一下 KV cache 是干什么的
2、写一个脚本：统计目录下每个文件的行数
（3）把这段话改得更简洁一点
• 为什么推理速度会随上下文变长而下降？
- 用三句话讲清楚什么是 MoE
6. 把这段中文翻译成地道的英文

希望这些对你有帮助。`;
ck("整段真实输出切出 6 条问题",
   eq(parseSuggestions(REAL), [
     "解释一下 KV cache 是干什么的",
     "写一个脚本：统计目录下每个文件的行数",
     "把这段话改得更简洁一点",
     "为什么推理速度会随上下文变长而下降？",
     "用三句话讲清楚什么是 MoE",
     "把这段中文翻译成地道的英文",
   ]),
   JSON.stringify(parseSuggestions(REAL)));

console.log("=".repeat(58));
if (FAILS.length) { console.log(`FAIL：${FAILS.length} 项未通过 -> ${JSON.stringify(FAILS)}`); process.exit(1); }
console.log("ALL PASS");
