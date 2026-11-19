// 产品名。改名字只改这一个文件 —— 页头、浏览器标题、导出文件名、关于页都读它。
// （唯一漏网的是 index.html 的 <title>：静态 HTML 没法 import，改名时记得一起改。）
//
// 取名 SSEStraBed / 层磐：
//   · Stra —— 承接原项目 Strata 的血统；
//   · Bed  —— 地质学里 bed 就是「岩层」，和 strata（层）本是同一套地层术语，
//             合起来正好描述这台机器上跑的东西：48 层堆起来的模型。
//             「层」这个字也不虚 —— 推理可视化那一页画的就是这 48 层。
//   · 中文「层磐」= 层理 + 磐石（坚如磐石）：有层次，也稳。
export const NAME = "SSEStraBed";
export const NAME_ZH = "层磐";

/** 导出对话时的文件名前缀（只用 ASCII，避免各种系统上的文件名问题） */
export const FILE_SLUG = "ssestrabed";
