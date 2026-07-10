---
name: podcast-rough-cut
description: "Transcribe long podcasts, review cut candidates, apply buffered edits, normalize loudness, and generate auditable cut reports without overwriting source audio."
category: content
risk: critical
source: https://github.com/smwswk/xiaoming-agent-skills/tree/main/skills/podcast-rough-cut
source_repo: smwswk/xiaoming-agent-skills
source_type: community
date_added: "2026-07-10"
author: smwswk
tags: [podcast, audio, ffmpeg, asr, editing]
tools: [claude-code, codex-cli, cursor, gemini-cli]
license: "MIT"
license_source: "https://github.com/smwswk/xiaoming-agent-skills/blob/main/LICENSE"
---

# 播客粗剪与压限工作流

## Overview

本技能用于把长播客素材整理成可复听、可继续精修的粗剪版本。它覆盖从音视频抽音频、ASR 转写、321 重启标记识别、候选剪辑点生成、剪辑判断、1 秒缓冲切口、剪辑点静音缺口标记、重新编码、单声道压限均衡、剪映黑场 MP4 到验收报告的完整流程。

## Best Practices

- 本技能服务「粗剪和技术处理」，最终节目叙事判断仍由 Codex 和花盆复听决定。
- 默认从原始音频重新剪，不沿用已经剪坏的中间版本，不覆盖原始音频。
- 默认保留每个剪辑点前后各 `1` 秒修复余量：实际删除范围为 `start + buffer_seconds` 到 `end - buffer_seconds`。
- 默认不做强降噪、不自动删静音，避免破坏停顿、笑声和情绪节奏。
- 单声道素材无法真正分离两位主讲人。本技能只能做整体动态均衡、压缩、限幅和响度标准化；如果仍然一人偏小，下一步应做「按说话人手工分段增益」。
- 剪辑点标记默认关闭。需要在后期软件里按波形定位切口时，使用 `--splice-marker-mode silence-insert --splice-silence-duration 2` 生成静音缺口定位版。
- 静音缺口版会改变总时长，只用于定位和修复切口，不适合直接发布或作为最终复听版。
- 不再使用提示音做剪辑点定位。旧参数 `--mark-splices` 仅作为兼容入口，脚本会映射为静音缺口。
- 非 dry-run 成功输出粗剪音频后，默认额外生成同名 `_剪映黑场.mp4`，用于剪映/CapCut 时间线吸附；如不需要，显式加 `--no-black-video`。
- 321 重启识别依赖 SRT 时间码。识别到 `321 / 3 2 1 / 三二一` 后，只剪 `321` 前面能和 `321` 后面新 take 对上的重复文本；找不到可靠重复起点时不剪。

## When to Use This Skill

收到以下任务时使用本技能：

- 「把播客音频识别成文字」「生成全文转写」「转 SRT」「给我带时间码的文字稿」
- 播客、音频、视频语境下的「粗剪」「音频粗剪」「视频粗剪」「播客粗剪」「试剪」「剪一下」「重剪」「粗剪这类词」
- 「按剪辑点剪一下」「从原音频重剪」「先扫候选剪辑点」
- 「切口前后保留一秒」「给我剪辑清单」
- 「激进版剪坏了，按信息约简重新剪」
- 「不要切断笑声」「不要拆完整故事」「上下文要接上」
- 「在剪辑点做一个标记」「波形上能看出来」「插入静音缺口」「后期软件里修复切口」
- 「识别 321 重启」「录制过程中 321 重新来」「把 321 前面重复的部分剪掉」
- 「做压限」「响度标准化」「两个人声音不均衡」「我的声音比嘉宾大」

## 推荐目录

接触印相节目默认使用：

```text
$HOME/Movies/接触印相/YYMMDD接触{嘉宾或主题}/
├── audio/
│   ├── {主题}_原始音频.m4a
│   ├── {主题}_试剪_信息约简版_压限.m4a
│   ├── {主题}_试剪_信息约简版_压限_剪映黑场.mp4
│   ├── {主题}_试剪_信息约简版_压限_静音缺口标记.m4a
│   ├── {主题}_试剪_信息约简版_压限_剪辑清单.txt
│   └── {主题}_试剪_信息约简版_压限_响度报告.txt
└── transcripts/
    ├── {主题}_完整转写.srt
    ├── {主题}_完整转写.txt
    └── {主题}_剪辑标注稿.md
```

## How It Works

### 1. 准备音频和转写

如果用户给的是视频，先抽出音频，原视频不覆盖。若已有原始音频，直接使用原始音频。

转写优先继承 media 工作区现有 ASR 工具：

```bash
python3 ~/.agents/skills/shownotes/scripts/siliconflow_asr.py "<audio.m4a>" "<完整转写.srt>" --format srt
python3 ~/.agents/skills/shownotes/scripts/siliconflow_asr.py "<audio.m4a>" "<完整转写.txt>" --format text
```

也可以通过本技能脚本代调：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<audio-or-video>" \
  --transcribe \
  --transcript-dir "<transcripts-dir>"
```

如果要绕过外部 ASR 脚本，使用内置直连硅基流动 ASR：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<audio-or-video>" \
  --transcribe \
  --direct-asr \
  --transcript-dir "<transcripts-dir>"
```

执行 ASR 前确认环境变量 `SILICONFLOW_API_KEY` 已设置，也可使用 `SF_KEY` 或 `~/.config/siliconflow/api_key`。外部 ASR 脚本不可用时，`--direct-asr` 可直连硅基流动 TeleAI/TeleSpeechASR，长音频会自动切片，并输出 SRT、TXT 和 JSON。

### 2. 形成剪辑段

阅读完整转写稿、旧标注和用户反馈，生成 `cuts.tsv`。每行只写确定要删除的完整段落：

```text
start	end	reason
00:00:00	00:04:42	录音设置
01:02:55	01:08:54	完整支线，删除后上下文可接
```

粗剪判断遵守以下原则：

- 剪后文本必须能独立阅读通顺。删除任何段落前，先把剪前最后一句和剪后第一句连读；没听过原素材的人也必须能理解上下文。
- 后文回指必须保护。若删除段后 30-60 秒内出现「刚刚说到」「所以」「因此」「这个地方」「这种状态」「这个问题」「刚才讲到」等回指，前文事实铺垫不能删光。
- 人物转折链必须保护。凡是包含「触发事件 -> 内心判断 -> 行动选择 -> 后续方向」的段落，即使出现年份、机构、训练、奖项、地名，也不能按普通履历细节整段删除。
- `可选压缩` 不等于 `整段删除`。可选压缩段默认只做局部剪枝，只有明确标注「可整段删除」并通过文本连读检查，才能写入 cuts。
- 不切断笑声、情绪尾巴和调侃；剪辑点附近有「哈哈哈」、明显笑声或情绪承接时，扩大到整段保留或整段删除。
- 不拆完整故事。故事只做整段保留或整段删除，避免中间抽走造成上下文断裂。
- 剪前剪后文本必须能自然接上。若语义承接不顺，标为「需人工复听」或扩大剪辑范围。
- 情感浓度高、人物关系、真诚判断、关键停顿和现场感优先保留。
- 欢乐有趣、能体现嘉宾性格的段落优先保留。
- 不追求极限时长，先保证听感和叙事连贯。

如果录制中使用「321」作为重启标记，可以让脚本从 SRT 自动生成重启剪辑段：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --restart-transcript "<完整转写.srt>" \
  --restart-cuts-output "<321重启剪辑段.tsv>" \
  --dry-run
```

321 重启剪辑规则：

- 扫描 `321 / 3 2 1 / 三二一`。
- 以 `321` 后面的新 take 文本为参照，在 `321` 前面寻找重复文本起点。
- 找到可靠重复起点时，生成从该起点到 `321` 标记后的剪辑段；实际执行仍按 1 秒缓冲少删两端。
- 找不到可靠重复起点时，不生成剪辑段，不做固定秒数盲剪。
- `--restart-cuts-output <cuts.tsv>` 可单独导出自动识别结果，便于复核。

如果只是想从完整转写稿里生成「候选剪辑点」，而不是直接剪，可以使用：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --suggest-cuts "<完整转写.srt>" \
  --suggest-cuts-output "<候选剪辑段.tsv>"
```

候选剪辑点会同时写出 TSV 和 JSON，包含 321 重启候选与口头禅候选。候选段不会自动加入实际剪辑；必须人工复核后再作为 `--cuts` 输入。

转写后立即生成候选剪辑点：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --transcribe \
  --direct-asr \
  --suggest-cuts \
  --transcript-dir "<transcripts-dir>"
```

重启剪辑可以和人工 cuts 合并执行：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --cuts "<人工确认删除段.tsv>" \
  --restart-transcript "<完整转写.srt>" \
  --output "<试剪_信息约简版_压限.m4a>"
```

### 3. 执行粗剪和压限

使用脚本执行剪辑、拼接、压限和响度标准化：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --cuts "<cuts.tsv>" \
  --output "<试剪_信息约简版_压限.m4a>"
```

常用参数：

- `--buffer-seconds 1`：每个删除段前后保留 1 秒，默认值。
- `--dry-run`：只计算实际删除段、保留段和预计时长，不生成音频。
- `--no-processing`：只剪辑拼接，不做动态均衡和响度标准化。
- `--splice-marker-mode silence-insert`：在每个拼接点额外插入静音缺口，方便在后期软件中看波形定位。
- `--splice-silence-duration 2`：每个剪辑点插入 2 秒静音；会累计增加总时长。
- `--target-lufs -16`：播客听感目标响度，默认 `-16 LUFS`。
- `--true-peak -1.5`：真峰值目标，默认 `-1.5 dBTP`。
- `--lra 11`：响度范围目标，默认 `11`。
- `--direct-asr`：不用外部 ASR 脚本，直接调用硅基流动 TeleAI/TeleSpeechASR。
- `--direct-asr-segment-seconds 3000`：内置 ASR 的切片长度，默认 `3000` 秒。
- `--suggest-cuts <srt>`：从 SRT 生成候选剪辑段，不自动执行。
- `--suggest-cuts-output <cuts.tsv>`：候选剪辑段 TSV 输出路径。
- `--max-filler-suggestions 20`：最多输出多少个口头禅候选段。
- `--no-black-video`：不生成默认剪映黑场 MP4。
- `--black-video-output <mp4>`：指定剪映黑场 MP4 输出路径。
- `--black-video-width 16 --black-video-height 16 --black-video-fps 25`：黑场视频画面参数。默认 `16x16`、`25fps`，用于保证 MP4 容器时长和音频对齐。

需要后期修复切口时，使用静音缺口定位版本：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --cuts "<cuts.tsv>" \
  --output "<试剪_信息约简版_压限_静音缺口标记.m4a>" \
  --splice-marker-mode silence-insert \
  --splice-silence-duration 2
```

静音缺口会直接插入输出音频，每个拼接点增加一段清晰空白波形；它会改变总时长，并让后续时间码累计后移。剪辑清单会额外写出每个静音缺口的开始、结束、对应原音频左右两侧衔接点和累计偏移。这一版本只用于后期定位和修复切口。

默认处理链：

```text
dynaudnorm -> acompressor -> alimiter -> loudnorm
```

用途分别是：缩小大声和小声的差距、轻压缩较大的声音、避免爆音、统一播客听感响度。

### 4. 验收

完成后必须检查：

- 原始音频仍然存在，未被覆盖。
- 输出 M4A 存在，时长符合预计。
- 默认剪映黑场 MP4 存在，视频/音频时长与 M4A 成品一致；如果使用 `--no-black-video`，最终回复要说明已跳过。
- 剪辑清单记录原始删除段、1 秒缓冲后的实际删除段、节省时长和删除理由。
- 抽查每个剪辑点前后文本，确认剪后文本可独立阅读通顺，后文没有因前文铺垫被删而产生困惑。
- 如使用 `--splice-marker-mode silence-insert`，剪辑清单包含「静音缺口标记点」表；输出总时长应增加 `剪辑点数量 x 静音秒数`。
- 如使用 `--restart-transcript`，检查自动生成的 321 重启剪辑段是否只覆盖 `321` 前面重复旧 take；未匹配的 321 不应产生剪辑段。
- 响度报告包含原始音频和输出音频的 LUFS、True Peak、LRA。
- 每个拼接点附近可正常解码。
- 抽听重点切口：开头、中段、结尾各至少 1 个；所有涉及笑声、情绪和完整故事的切口应优先复听。

## Examples

先做只读预演，检查删除范围与预计时长，不生成新音频：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --cuts "<人工确认删除段.tsv>" \
  --dry-run
```

人工确认后，从原始音频生成粗剪成品和审计报告：

```bash
python3 scripts/podcast_rough_cut.py \
  --input "<原始音频.m4a>" \
  --cuts "<人工确认删除段.tsv>" \
  --output "<试剪_信息约简版_压限.m4a>"
```

## Limitations

- 需要本机已安装 Python 3、`ffmpeg` 和 `ffprobe`；Windows 环境需要自行适配命令和路径。
- ASR 结果可能漏字、错字或错配时间码，任何自动候选剪辑段都必须人工复核。
- 单声道音频不能真正分离说话人；整体压限无法替代分轨录音或按说话人手工调增益。
- 自动 321 重启识别只处理有可靠重复文本的片段，不会用固定秒数盲剪。
- 静音缺口定位版会改变总时长，只能用于定位切口，不能作为发布母版。
- 本技能完成粗剪和技术处理，不替代节目叙事、事实核对、版权或发布判断。

## Security & Safety Notes

- 只处理用户拥有或明确获准处理的音视频；外部 ASR 会上传音频内容，执行前应取得用户同意。
- 默认先运行 `--dry-run`，展示输入、输出、剪辑段和预计时长，再生成文件。
- 永不覆盖原始素材；输出必须写入新路径，并在完成后验证原文件仍存在。
- 候选剪辑点不是执行授权。只有人工确认的 TSV 才能作为实际 `--cuts` 输入。
- API 密钥只从环境变量、钥匙串或受限配置文件读取，不写入命令示例、日志、报告或版本库。
- 不自动删除中间文件或原始文件；清理动作必须单独列清单并获得用户确认。

## Common Pitfalls

- 直接在已经剪坏的中间版本上继续剪，导致错误累积；应始终回到原始音频重建。
- 把口头禅候选、321 候选当作确定删除段；它们只能进入人工审核清单。
- 忽略切口前后的笑声、回指和故事铺垫，造成语义跳跃或情绪被截断。
- 把静音缺口定位版当成最终节目，导致时长和时间码整体偏移。
- 只看输出文件存在，不检查时长、响度报告和切口附近是否可正常解码。

## Output Delivery

最终回复用户时说明：

- 生成了哪些文件。
- 输出音频时长。
- 剪映黑场 MP4 路径和时长；如跳过则说明。
- 删除了多少段、实际节省多少。
- 是否开启剪辑点标记；如使用静音缺口，说明会改变总时长且只用于后期定位。
- 是否启用 321 重启识别；如启用，说明自动生成了多少个重启剪辑段。
- 音频处理后的响度变化。
- 哪些切口仍建议人工复听。
