// 由 tools/make-model-info.py 生成 —— 不要手改，下次生成会覆盖。
// 数据全部读自本机：GGUF 头的架构元数据 + 同一份头里每个张量的真实形状。
// 重新生成：python3 tools/make-model-info.py

export const LOCAL_MODEL = {
  "id": "local",
  "name": "Qwen3.8-Flash-Next GSQ-RCO",
  "note": "本机在跑 · 架构与参数量读自模型文件本身",
  "source": "local",
  "layers": 48,
  "hiddenSize": 2560,
  "heads": 24,
  "kvHeads": 2,
  "keyLength": 256,
  "valueLength": 256,
  "vocabSize": 248320,
  "intermediateSize": 640,
  "expertCount": 512,
  "expertUsed": 10,
  "expertFfn": 640,
  "sharedExpertFfn": 640,
  "contextLength": 262144,
  "fullAttentionInterval": 4,
  "hyperCount": 4,
  "linearAttnLayers": 36,
  "fullAttnLayers": 12,
  "layerKinds": [
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full",
    "ssm",
    "ssm",
    "ssm",
    "full"
  ],
  "ropeFreqBase": 10000000.0,
  "paramCount": 176943899520,
  "paramActive": 6035596160,
  "paramBreakdown": {
    "moe_experts": {
      "params": 120795955200,
      "share": 0.682679400237511
    },
    "per_layer_embed": {
      "params": 51200245760,
      "share": 0.289358637957523
    },
    "attention": {
      "params": 2424674304,
      "share": 0.013703068094336525
    },
    "embed": {
      "params": 1271398400,
      "share": 0.00718531920822901
    },
    "linear_attention": {
      "params": 576561024,
      "share": 0.0032584396837870705
    },
    "hyper": {
      "params": 323594240,
      "share": 0.0018287956853998467
    },
    "shared_expert": {
      "params": 236052480,
      "share": 0.001334052660986591
    },
    "router": {
      "params": 62914560,
      "share": 0.0003555621876237036
    },
    "indexer": {
      "params": 19663872,
      "share": 0.00011113054506734994
    },
    "norms": {
      "params": 30720,
      "share": 1.7361434942563652e-07
    }
  },
  "unclassified": [
    "blk.1.ple_conv1d.weight",
    "blk.1.ple_key.weight",
    "blk.1.ple_value.weight"
  ],
  "onDiskBytes": 75839998528,
  "quant": 23,
  "quantizedBy": "ISTA DASLab",
  "repoUrl": "https://huggingface.co/ISTA-DASLab",
  "sizeLabel": "512x56B",
  "ggufArch": "qwen4exp",
  "ggufDescription": "A Preview of the Qwen4 Architecture",
  "layerTensors": {
    "ssm": {
      "attn": [
        [
          "blk.{i}.attn_gate.weight",
          [
            2560,
            6144
          ]
        ],
        [
          "blk.{i}.attn_qkv.weight",
          [
            2560,
            10240
          ]
        ],
        [
          "blk.{i}.ssm_a",
          [
            48
          ]
        ],
        [
          "blk.{i}.ssm_alpha.weight",
          [
            2560,
            48
          ]
        ],
        [
          "blk.{i}.ssm_beta.weight",
          [
            2560,
            48
          ]
        ],
        [
          "blk.{i}.ssm_conv1d.weight",
          [
            4,
            10240
          ]
        ],
        [
          "blk.{i}.ssm_dt.bias",
          [
            48
          ]
        ],
        [
          "blk.{i}.ssm_out.weight",
          [
            6144,
            2560
          ]
        ]
      ],
      "ffn": [
        [
          "blk.{i}.ffn_down_exps.weight",
          [
            640,
            2560,
            512
          ]
        ],
        [
          "blk.{i}.ffn_down_shexp.weight",
          [
            640,
            2560
          ]
        ],
        [
          "blk.{i}.ffn_gate_exps.weight",
          [
            2560,
            640,
            512
          ]
        ],
        [
          "blk.{i}.ffn_gate_inp.weight",
          [
            2560,
            512
          ]
        ],
        [
          "blk.{i}.ffn_gate_inp_shexp.weight",
          [
            2560
          ]
        ],
        [
          "blk.{i}.ffn_gate_shexp.weight",
          [
            2560,
            640
          ]
        ],
        [
          "blk.{i}.ffn_up_exps.weight",
          [
            2560,
            640,
            512
          ]
        ],
        [
          "blk.{i}.ffn_up_shexp.weight",
          [
            2560,
            640
          ]
        ]
      ],
      "hyper": [
        [
          "blk.{i}.hc_attn_down.weight",
          [
            10240,
            320
          ]
        ],
        [
          "blk.{i}.hc_attn_inject.weight",
          [
            10240,
            4
          ]
        ],
        [
          "blk.{i}.hc_attn_norm.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.hc_attn_up.weight",
          [
            320,
            10240
          ]
        ],
        [
          "blk.{i}.hc_ffn_down.weight",
          [
            10240,
            320
          ]
        ],
        [
          "blk.{i}.hc_ffn_inject.weight",
          [
            10240,
            4
          ]
        ],
        [
          "blk.{i}.hc_ffn_norm.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.hc_ffn_up.weight",
          [
            320,
            10240
          ]
        ]
      ],
      "ple": [
        [
          "blk.{i}.ple_conv1d.weight",
          [
            4,
            10240
          ]
        ],
        [
          "blk.{i}.ple_key.weight",
          [
            2560,
            10240
          ]
        ],
        [
          "blk.{i}.ple_norm_conv.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.ple_norm_key.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.ple_norm_query.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.ple_value.weight",
          [
            2560,
            2560
          ]
        ]
      ],
      "ln": [
        [
          "blk.{i}.ssm_norm.weight",
          [
            128
          ]
        ]
      ]
    },
    "full": {
      "attn": [
        [
          "blk.{i}.attn_k.weight",
          [
            2560,
            512
          ]
        ],
        [
          "blk.{i}.attn_output.weight",
          [
            6144,
            2560
          ]
        ],
        [
          "blk.{i}.attn_q.weight",
          [
            2560,
            12288
          ]
        ],
        [
          "blk.{i}.attn_v.weight",
          [
            2560,
            512
          ]
        ],
        [
          "blk.{i}.indexer.k_proj.weight",
          [
            2560,
            128
          ]
        ],
        [
          "blk.{i}.indexer.q_proj.weight",
          [
            2560,
            512
          ]
        ]
      ],
      "ln": [
        [
          "blk.{i}.attn_k_norm.weight",
          [
            256
          ]
        ],
        [
          "blk.{i}.attn_q_norm.weight",
          [
            256
          ]
        ],
        [
          "blk.{i}.indexer.k_norm.weight",
          [
            128
          ]
        ],
        [
          "blk.{i}.indexer.q_norm.weight",
          [
            128
          ]
        ]
      ],
      "ffn": [
        [
          "blk.{i}.ffn_down_exps.weight",
          [
            640,
            2560,
            512
          ]
        ],
        [
          "blk.{i}.ffn_down_shexp.weight",
          [
            640,
            2560
          ]
        ],
        [
          "blk.{i}.ffn_gate_exps.weight",
          [
            2560,
            640,
            512
          ]
        ],
        [
          "blk.{i}.ffn_gate_inp.weight",
          [
            2560,
            512
          ]
        ],
        [
          "blk.{i}.ffn_gate_inp_shexp.weight",
          [
            2560
          ]
        ],
        [
          "blk.{i}.ffn_gate_shexp.weight",
          [
            2560,
            640
          ]
        ],
        [
          "blk.{i}.ffn_up_exps.weight",
          [
            2560,
            640,
            512
          ]
        ],
        [
          "blk.{i}.ffn_up_shexp.weight",
          [
            2560,
            640
          ]
        ]
      ],
      "hyper": [
        [
          "blk.{i}.hc_attn_down.weight",
          [
            10240,
            320
          ]
        ],
        [
          "blk.{i}.hc_attn_inject.weight",
          [
            10240,
            4
          ]
        ],
        [
          "blk.{i}.hc_attn_norm.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.hc_attn_up.weight",
          [
            320,
            10240
          ]
        ],
        [
          "blk.{i}.hc_ffn_down.weight",
          [
            10240,
            320
          ]
        ],
        [
          "blk.{i}.hc_ffn_inject.weight",
          [
            10240,
            4
          ]
        ],
        [
          "blk.{i}.hc_ffn_norm.weight",
          [
            10240
          ]
        ],
        [
          "blk.{i}.hc_ffn_up.weight",
          [
            320,
            10240
          ]
        ]
      ]
    }
  },
  "meta": {
    "shards": [
      "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf",
      "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf"
    ],
    "ggufVersion": 3,
    "tensorCount": 1224,
    "archKeys": [
      "qwen4exp.attention.compress_ratios",
      "qwen4exp.attention.head_count",
      "qwen4exp.attention.head_count_kv",
      "qwen4exp.attention.indexer.head_count",
      "qwen4exp.attention.indexer.key_length",
      "qwen4exp.attention.indexer.top_k",
      "qwen4exp.attention.key_length",
      "qwen4exp.attention.layer_norm_rms_epsilon",
      "qwen4exp.attention.value_length",
      "qwen4exp.block_count",
      "qwen4exp.context_length",
      "qwen4exp.embedding_length",
      "qwen4exp.embedding_length_per_layer_input",
      "qwen4exp.expert_count",
      "qwen4exp.expert_feed_forward_length",
      "qwen4exp.expert_shared_feed_forward_length",
      "qwen4exp.expert_used_count",
      "qwen4exp.full_attention_interval",
      "qwen4exp.hyper_connection.count",
      "qwen4exp.hyper_connection.low_rank",
      "qwen4exp.ple.conv_kernel",
      "qwen4exp.ple.eos_token_id",
      "qwen4exp.ple.head_offsets",
      "qwen4exp.ple.head_vocab_sizes",
      "qwen4exp.ple.heads_per_ngram",
      "qwen4exp.ple.image_token_id",
      "qwen4exp.ple.layer_multipliers",
      "qwen4exp.ple.layers",
      "qwen4exp.ple.ngram_size",
      "qwen4exp.rope.dimension_count",
      "qwen4exp.rope.dimension_sections",
      "qwen4exp.rope.freq_base",
      "qwen4exp.ssm.conv_kernel",
      "qwen4exp.ssm.group_count",
      "qwen4exp.ssm.inner_size",
      "qwen4exp.ssm.state_size",
      "qwen4exp.ssm.time_step_rank"
    ]
  }
};

export default LOCAL_MODEL;
