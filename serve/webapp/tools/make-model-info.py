#!/usr/bin/env python3
"""把本机正在跑的那个模型，读成一个前端能直接 import 的模块。

为什么要这么做：推理可视化那一页原来用的是一个 12 层的假模型，跟这台机器上真跑的
（Qwen3.8-Flash-Next：48 层混合 MoE）对不上。而给引擎加 /api/models 这类路由要重启进程 =
断服，代价太大。好在**真话就摆在磁盘上**：

  · GGUF 头里嵌着完整的架构元数据（层数/宽度/头数/专家数/上下文……）；
  · 同一个头里还有**每个张量的真实形状** —— 参数量可以按类别真加出来，不是估的。

于是改成「构建时跑一次本脚本，生成 src/viz/model.local.js」。前端读它，不碰引擎。

用法：python3 tools/make-model-info.py
无参数时按 ~/Strata/strata-iq3_xxs.json 里的路径自动找模型。
"""
import argparse
import json
import math
import os
import struct
import sys

# ------------------------------------------------------------------ GGUF 头

_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_FMT = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


class _Reader:
    def __init__(self, fh):
        self.fh = fh

    def u32(self):
        return struct.unpack("<I", self.fh.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.fh.read(8))[0]

    def string(self):
        return self.fh.read(self.u64()).decode("utf-8", "replace")

    def value(self, t):
        if t == 8:
            return self.string()
        if t == 9:                      # 数组
            et = self.u32()
            n = self.u64()
            if et == 8:                 # 字符串数组（词表/merges）：只记长度，内容对界面没意义
                for _ in range(n):
                    self.fh.seek(self.u64(), 1)
                return {"__len__": n}
            return [self.value(et) for _ in range(n)]
        return struct.unpack("<" + _FMT[t], self.fh.read(_SIZES[t]))[0]


def read_gguf(path):
    """返回 (kv 元数据, [(张量名, [各维]), …])。张量表在 KV 段之后，顺序读。"""
    tensors = []
    with open(path, "rb") as fh:
        r = _Reader(fh)
        if fh.read(4) != b"GGUF":
            raise SystemExit(f"不是 GGUF 文件: {path}")
        version = r.u32()
        n_tensors = r.u64()
        n_kv = r.u64()
        kv = {}
        for _ in range(n_kv):
            key = r.string()
            kv[key] = r.value(r.u32())
        for _ in range(n_tensors):
            name = r.string()
            nd = r.u32()
            dims = [r.u64() for _ in range(nd)]
            r.u32()                     # 量化类型
            r.u64()                     # 数据偏移
            tensors.append((name, dims))
    return {"version": version, "kv": kv, "tensors": tensors}


# ------------------------------------------------------------------ 分类
#
# 分类口径**照着张量名来**，不猜。每一类在界面上都能点开看到是哪些张量。

def classify(name):
    if name.startswith("per_layer_token_embd"):
        return "per_layer_embed"                      # ple.* 那套逐层嵌入表
    if name.startswith("token_embd") or name.startswith("output.weight"):
        return "embed"
    if ".indexer." in name:
        return "indexer"                              # 全注意力层里的稀疏检索器
    if "ssm_" in name:
        return "linear_attention"                     # 36 层的线性注意力（状态空间）
    if "attn_" in name:
        return "attention"                            # 12 层的全注意力
    if "hc_" in name:
        return "hyper"                                # hyper-connections
    if "shexp" in name:
        return "shared_expert"
    if "gate_inp" in name:
        return "router"
    if name.startswith("blk.") and "ffn_" in name:
        return "moe_experts"
    if "norm" in name:
        return "norms"
    return "other"


# 每层的张量按角色分组，名字里的层号换成 {i} —— 前端照着它列「这一层有哪些权重矩阵」，
# 形状一律是文件里的真值。加新模型不用改前端。
# 顺序有意义：先匹配更专的，hc_ 那组超连接不该被 attn_ 抢走
LAYER_ROLES = (
    ("hyper", ("hc_",)),
    ("ple", ("ple_",)),
    ("ln", ("attn_norm", "ffn_norm", "sb_norm", "norm")),
    ("attn", ("attn_", "indexer.", "ssm_")),
    ("ffn", ("ffn_",)),
)


def layer_tensors(tensors):
    """{kind: {role: [[名字模板, [维度...]], ...]}}，取每种层里张量最全的那一层当样板。"""
    sample = {}                                  # kind -> {层号: {role: [...]}}
    for name, dims in tensors:
        if not name.startswith("blk."):
            continue
        parts = name.split(".")
        try:
            i = int(parts[1])
        except (IndexError, ValueError):
            continue
        kind = "ssm" if any("ssm_" in n for n, _ in [x for x in tensors if x[0].startswith(f"blk.{i}.")]) else "full"
        role = next((r for r, keys in LAYER_ROLES if any(k in name for k in keys)), None)
        if role is None:
            continue
        tmpl = name.replace(f"blk.{i}.", "blk.{i}.", 1)
        sample.setdefault(kind, {}).setdefault(i, {}).setdefault(role, []).append([tmpl, dims])
    out = {}
    for kind, per_layer in sample.items():
        best = max(per_layer.values(), key=lambda d: sum(len(v) for v in d.values()))
        out[kind] = best
    return out


def layer_kinds(tensors, n_layers):
    """每层用哪种注意力，判据是**有没有真投影权重**，不看遍历顺序：

    · 有 attn_q / attn_k / attn_v / attn_output（分开的 QKV 投影）= 全注意力层；
    · 有 ssm_a / ssm_out 那一组 = 线性注意力（状态空间）层。

    本机模型里两者是互斥的（12 + 36 = 48），而且 12 正好等于 full_attention_interval 的商，
    可以互相印证 —— 所以这个判据能自检。
    """
    kinds = ["?"] * n_layers
    per_layer = {}
    for name, _dims in tensors:
        if name.startswith("blk."):
            try:
                per_layer.setdefault(int(name.split(".")[1]), set()).add(name)
            except (IndexError, ValueError):
                pass
    for i, names in per_layer.items():
        if i >= n_layers:
            continue
        full = any(n.endswith(("attn_q.weight", "attn_k.weight", "attn_output.weight")) for n in names)
        linear = any("ssm_out.weight" in n or n.endswith("ssm_a") for n in names)
        if full:
            kinds[i] = "full"
        elif linear:
            kinds[i] = "ssm"
    return kinds


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.expanduser("~/Strata/strata-iq3_xxs.json"))
    ap.add_argument("--model-dir", default=None, help="默认从 config 的 --native 推")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "src", "viz", "model.local.js"))
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    native = next((a for i, a in enumerate(cfg["args"]) if i and cfg["args"][i - 1] == "--native"), None)
    if not native:
        raise SystemExit("配置里找不到 --native")
    model_dir = args.model_dir or os.path.dirname(os.path.realpath(native))

    # 分片模型：把目录下所有非 mmproj 的 gguf 都读了，张量表拼起来才是完整的
    shards = sorted(f for f in os.listdir(model_dir) if f.endswith(".gguf") and "mmproj" not in f)
    if not shards:
        raise SystemExit(f"{model_dir} 下没有 .gguf")
    kv, tensors, version = None, [], None
    for s in shards:
        got = read_gguf(os.path.join(model_dir, s))
        kv = kv or got["kv"]
        version = version or got["version"]
        tensors += got["tensors"]
    on_disk = sum(os.path.getsize(os.path.join(model_dir, s)) for s in shards)

    arch = kv["general.architecture"]
    g = lambda suffix, default=None: kv.get(f"{arch}.{suffix}", default)

    n_layers = g("block_count")
    heads = g("attention.head_count")
    kv_heads = g("attention.head_count_kv", heads)
    vocab = kv.get("tokenizer.ggml.tokens")
    vocab = vocab.get("__len__") if isinstance(vocab, dict) else None
    expert_count, expert_used = g("expert_count"), g("expert_used_count")
    kinds = layer_kinds(tensors, n_layers)

    by_bucket, other_names = {}, []
    for name, dims in tensors:
        b = classify(name)
        by_bucket[b] = by_bucket.get(b, 0) + math.prod(dims)
        if b == "other":
            other_names.append(name)
    total = sum(by_bucket.values())

    # 每个 token 实际参与矩阵乘的参数：MoE 只算命中的那几个专家，嵌入是查表不计。
    active = total - by_bucket.get("embed", 0) - by_bucket.get("per_layer_embed", 0)
    experts = by_bucket.get("moe_experts", 0)
    if experts and expert_count and expert_used:
        active = active - experts + experts * (expert_used / expert_count)

    def bn(key):
        v = by_bucket.get(key, 0)
        return {"params": v, "share": (v / total) if total else 0}

    out = {
        "id": "local",
        "name": kv.get("general.name", "local model"),
        "note": "本机在跑 · 架构与参数量读自模型文件本身",
        "source": "local",
        "layers": n_layers,
        "hiddenSize": g("embedding_length"),
        "heads": heads,
        "kvHeads": kv_heads,
        "keyLength": g("attention.key_length"),
        "valueLength": g("attention.value_length"),
        "vocabSize": vocab,
        "intermediateSize": g("expert_shared_feed_forward_length") or g("expert_feed_forward_length"),
        "expertCount": expert_count,
        "expertUsed": expert_used,
        "expertFfn": g("expert_feed_forward_length"),
        "sharedExpertFfn": g("expert_shared_feed_forward_length"),
        "contextLength": g("context_length"),
        "fullAttentionInterval": g("full_attention_interval"),
        "hyperCount": g("hyper_connection.count"),
        "linearAttnLayers": kinds.count("ssm"),
        "fullAttnLayers": kinds.count("full"),
        "layerKinds": kinds,
        "ropeFreqBase": g("rope.freq_base"),
        "paramCount": total,
        "paramActive": int(active),
        "paramBreakdown": {k: bn(k) for k in
                           ("moe_experts", "per_layer_embed", "attention", "embed", "linear_attention",
                            "hyper", "shared_expert", "router", "indexer", "norms")},
        "unclassified": other_names[:20],       # 没归好类的张量名，宁可摆出来也别悄悄塞进「其他」
        "onDiskBytes": on_disk,
        "quant": kv.get("general.file_type"),
        "quantizedBy": kv.get("general.quantized_by"),
        "repoUrl": kv.get("general.repo_url"),
        "sizeLabel": kv.get("general.size_label"),
        "ggufArch": arch,
        "ggufDescription": kv.get("general.description"),
        # 出处：界面上要能点开看到这些原始键与文件，别让推导出来的东西看起来像实测
        "layerTensors": layer_tensors(tensors),
        "meta": {
            "shards": shards,
            "ggufVersion": version,
            "tensorCount": len(tensors),
            "archKeys": sorted(k for k in kv if k.startswith(arch + ".")),
        },
    }

    js = ("// 由 tools/make-model-info.py 生成 —— 不要手改，下次生成会覆盖。\n"
          "// 数据全部读自本机：GGUF 头的架构元数据 + 同一份头里每个张量的真实形状。\n"
          "// 重新生成：python3 tools/make-model-info.py\n\n"
          "export const LOCAL_MODEL = "
          + json.dumps(out, ensure_ascii=False, indent=2) + ";\n\n"
          "export default LOCAL_MODEL;\n")
    dest = os.path.normpath(args.out)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(js)

    print(f"模型   : {out['name']} ({arch})  {len(shards)} 个分片 / {len(tensors)} 个张量")
    print(f"层数   : {n_layers}（线性注意力 {out['linearAttnLayers']} + 全注意力 {out['fullAttnLayers']}）"
          f"  隐藏 {out['hiddenSize']}  头 {heads}(KV {kv_heads})  词表 {vocab}")
    print(f"专家   : {expert_count} 取 {expert_used}  共享专家 FFN {out['sharedExpertFfn']}")
    print(f"参数   : 总 {total/1e9:.1f}B   每 token 活跃约 {active/1e9:.2f}B   磁盘 {on_disk/2**30:.1f} GiB")
    print("分组   :")
    for k, v in sorted(by_bucket.items(), key=lambda x: -x[1])[:10]:
        print(f"         {k:18} {v/1e9:8.2f}B  {100*v/total:5.1f}%")
    print(f"→ {dest}")


if __name__ == "__main__":
    sys.exit(main())
