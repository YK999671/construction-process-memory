# Construction Process Memory / 施工过程证据记忆

A local-first Codex skill for organizing construction progress evidence and answering questions in chat. It indexes site photos in SQLite, preserves human confirmations separately from model observations, and uses drawings and project documents as evidence when available.

这是一个以 Codex 对话为入口的本地工程过程记忆 Skill。照片原件和 SQLite 数据库存放在用户指定的项目目录，不随本仓库上传。

## Current capabilities / 当前已实现

- Incremental photo import with SHA-256 deduplication, EXIF or file-time provenance, and local copies of originals.
- Candidate observations of visible construction state, plus separate user confirmation of project scope, date, location, and observation.
- Multiple building labels on one aerial photo, model location candidates, before/after photo comparisons, and an audit trail.
- Search by date, confirmed project scope (`--scope`), location, photo ID, or keyword.
- One-way Obsidian export of the SQLite photo index, thumbnails, and a navigation canvas.

The model analyzes images and writes candidate observations through the CLI; the CLI itself does **not** perform automatic computer vision or CAD recognition. Dates from EXIF, filenames, and watermarks remain clues until verified. A confirmed project scope does not confirm a precise floor or axis.

模型负责看图并通过 CLI 写入候选观察；脚本本身不自动识图或解析 CAD。EXIF、文件名和水印时间在核实前只是线索。确认项目范围不等于确认具体楼层或轴线。

## Planned extensions / 预留能力

Batch confirmation cards, persistent import of daily reports and meeting minutes, drawing-version links, and evidence coverage statistics are specified in [the evidence workflow](references/evidence-workflow.md) but are **not yet implemented** as CLI commands or SQLite tables. Do not treat those specifications as shipped features.

批次确认卡、日报和会议纪要长期入库、图纸版本关联、资料覆盖统计已有工作规则，尚未在当前 CLI 和数据库中实现。

## Install / 安装

Copy or clone this folder to your Codex skills directory as `construction-process-memory`. Python 3.10+ is required. [Pillow](https://pillow.readthedocs.io/) is optional for image verification, dimensions, EXIF, and Obsidian thumbnails:

```powershell
python -m pip install Pillow
```

Read [SKILL.md](SKILL.md) for the Codex workflow. The CLI stores its project configuration at `$CODEX_HOME/construction-memory/config.json` by default, or at `CONSTRUCTION_MEMORY_CONFIG` when set. Keep that configuration and project data outside this repository.

```powershell
python scripts/construction_memory.py configure --project "项目名称" --photos "照片文件夹" --data-dir "项目数据目录"
python scripts/construction_memory.py ingest --project "项目名称"
python scripts/construction_memory.py status --project "项目名称"
python scripts/construction_memory.py search --project "项目名称" --scope "已确认范围" --limit 100
```

For a photo observation, create a UTF-8 JSON file with `photo_id`, `observed_activity`, `observed_state`, `location_clues`, and `uncertainties`, then run `record --json <path>`. Human corrections use `review --json <path>`; only include fields actually confirmed by the reviewer. See [SKILL.md](SKILL.md) for the supported commands and evidence rules.

## Data boundary / 数据边界

This repository contains code and workflow instructions only. Never commit real project photos, drawings, databases, configuration files, or review exports without the project owner's separate decision to publish them. The skill makes no engineering acceptance or completion-rate determination from photos alone.

本仓库仅公开代码与流程规则。实际项目照片、图纸、数据库、配置和审核导出资料不在仓库内；照片可见形态不等于验收完成。

## 使用与分发许可

允许免费安装、内部调整、用于收费项目及交付项目成果。免费分享修改版时须标明修改情况并保留声明。未经书面许可，不得出售本许可涵盖的 Skill 或修改版，也不得将其捆绑在付费产品中交付。 完整条款见 [LICENSE](LICENSE)。

[此前按 MIT 发布的内容](https://github.com/YK999671/construction-process-memory/tree/904bf28bba2421fefa88baafd194f5b76d44b102)继续遵循 [MIT 原文](LICENSE-MIT)。新版限制仅适用于授权更新后的新增原创部分。
