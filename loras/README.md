# LoRA 清单

每个模型一个文件，`imggen` / `imgpool` / `lora-db` 共用。

```
loras/
  krea2.json      Krea 2
  moss.json       MOSS Image
  anima.json      Anima
  minimax.json    MiniMax H3
  flux.json       FLUX.1 dev（当前 imggen 不支持，仅登记）
```

## 分类

`kind` 只有两个值：

| kind | 含义 | 例子 |
|---|---|---|
| `functional` | 改变生成流程或成片质量，不改变画面主题 | 加速、编辑、细节滑块、成片增强、局部增强 |
| `style` | 改变画面长相：画风、写实度、角色、主题 | 厚涂、半写实、蔚蓝档案、角色 LoRA |

角色类 LoRA 归 `style`，用 `tags: ["character"]` 标注。这样 `functional`
始终是"工具"，`style` 始终是"内容/长相"。

## 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `file` | string | 文件名，必须与 `ComfyUI/models/loras/` 下的文件一致 |
| `kind` | `functional` / `style` | 一级分类 |
| `description` | string | 一句话说明它做什么、什么时候用 |
| `tags` | string[] | 细分标签，如 `detail`、`anime`、`character`、`unverified` |
| `aliases` | string[] | `--lora` 可用的短名，如 `thick`、`ur` |
| `default_strength` | number \| null | 不写强度时的默认权重 |
| `recommended` | string \| null | 推荐用法或组合 |
| `trigger` | string \| null | 触发词（如果作者要求） |
| `license` | string | `unknown` / `mit` / `cc-by` / `civitai-*` 等 |
| `commercial_ok` | true \| false \| null | 是否可商用；`null` = 未确认 |
| `source` | string \| null | 来源页 |
| `tested` | string[] | 本地实测记录，一条一行 |
| `verified` | boolean | 描述是否可靠。`false` 会出现在 `audit` 里 |

`description` 只写能确认的事实。没跑过、没查到出处的写"待核实"，并把
`verified` 设为 `false`，不要编造推荐权重。

## 维护

```bash
bin/lora-db list                      # 全部模型，按 functional/style 分组
bin/lora-db list --model krea2        # 单个模型
bin/lora-db list --kind style --json  # 机器可读
bin/lora-db info thick                # 单条详情
bin/lora-db audit                     # 对比磁盘，找未登记/缺失/未验证
bin/lora-db add new.safetensors --model krea2 --kind style --desc "..."
bin/lora-db set thick --desc "..." --strength 0.6 --verified
bin/lora-db remove thick --model krea2
bin/lora-db render --model krea2      # 生成 Markdown 表
```

`audit` 的磁盘目录按 `COMFY_LORA_DIR` > `COMFY_DIR/models/loras` >
`COMFY_ROOT/ComfyUI/models/loras` > 从当前目录向上查找。

## 与 imggen 的关系

- `--lora thick:0.6` 的别名来自这里；JSON 缺失时回退到 `imggen` 内置的 6 个别名
- `default_strength` 决定 `--lora thick`（不写强度）时的权重
- LoRA 登记在 A 模型、却用 B 模型加载时，`imggen` 在 stderr 打一条警告，不阻断
- `imggen --list` 的 `lora_groups` 字段就是这份数据的快照
