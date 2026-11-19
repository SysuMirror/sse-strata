// 推理可视化的数据契约。
//
// 这些结构**同时**是后端接口的返回格式（见 api.js）：换真实接口时形状不变，
// 前端一行都不用改。用 JSDoc 而不是 .ts —— 本工程其余部分都是纯 JS，
// 引入 tsc/vue-tsc 会多出一套工具链；JSDoc 一样能被编辑器做类型检查与补全。
//
// 建议用 `import("./types.js").GraphNode` 在注释里引用这些类型：
//   /** @type {import("./types.js").GraphNode[]} */

/**
 * 一个可被可视化演示的模型（管理员注册好的那份列表里的一项）。
 * @typedef {Object} ModelInfo
 * @property {string} id            稳定标识，接口路径里用
 * @property {string} name          展示名
 * @property {number} paramCount    总参数量（不含嵌入时可只算主干，见 mock.js 的口径说明）
 * @property {number} layers        Transformer 层数
 * @property {number} hiddenSize    隐藏维度 d_model
 * @property {number} heads         注意力头数
 * @property {number} vocabSize     词表大小
 * @property {number} [intermediateSize] FFN 中间维度，缺省按 4×hiddenSize 估
 * @property {string} [note]        一句话说明，卡片上用
 */

/** 图节点类型。决定几何体形状、配色与图例分组。 */
export const NODE_TYPES = ["input", "tokenizer", "embedding", "layernorm", "attention", "linear", "ffn", "residual", "output"];

/**
 * 模型图里的一个节点。
 * @typedef {Object} GraphNode
 * @property {string} id
 * @property {string} label
 * @property {("input"|"tokenizer"|"embedding"|"layernorm"|"attention"|"linear"|"ffn"|"residual"|"output")} type
 * @property {[number, number, number]} position 场景坐标（Y 轴向上，主干沿 Y 排布）
 * @property {number} size                       几何体尺寸基数，由参数量推出来
 * @property {number} [paramCount]
 * @property {string} [inputShape]
 * @property {string} [outputShape]
 * @property {number} [layer]                    属于第几层（0 起；非 Transformer 层为 undefined）
 * @property {string[]} [weightNames]            这个节点持有的权重名，供右侧面板列矩阵形状
 * @property {import("./types.js").WeightInfo[]} [weights]
 */

/**
 * 权重矩阵的元信息。
 * @typedef {Object} WeightInfo
 * @property {string} name
 * @property {string} shape          如 "[768, 768]"
 * @property {number} paramCount
 */

/**
 * 图里的一条连线。
 * @typedef {Object} GraphEdge
 * @property {string} id
 * @property {string} from           起点节点 id
 * @property {string} to             终点节点 id
 * @property {string} [weightName]   这条边代表的权重（没有就是纯数据流）
 * @property {("data"|"residual"|"output")} [kind]
 */

/**
 * 一个演示步骤：推理过程被拆成的第 i 帧。
 * @typedef {Object} InferenceStep
 * @property {number} id
 * @property {string} name
 * @property {string} description
 * @property {string[]} activeNodeIds
 * @property {string[]} activeEdgeIds
 * @property {string[]} activeWeightNames
 * @property {string} tensorShape    这一步张量形状，如 "[7, 768]"
 * @property {number} paramCount     这一步用到的参数量
 * @property {number} flops          这一步的浮点运算量估算
 * @property {number} [layer]        第几层（用于「层 3 / 12」这类进度显示）
 * @property {("input"|"tokenize"|"embed"|"attention"|"layernorm"|"ffn"|"residual"|"output")} [kind]
 */

/**
 * 数据来源。界面右上角按**当前选中的模型**如实标注：
 *   api   命中后端接口
 *   local 从本机模型文件里读出来的真值（GGUF 元数据 + 张量形状）
 *   mock  示例数据，用来演示不同规模
 */
export const SOURCE = { API: "api", LOCAL: "local", MOCK: "mock" };

export default {};
