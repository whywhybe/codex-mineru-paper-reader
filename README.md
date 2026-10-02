# codex-mineru-paper-reader

通过 MinerU 官方精准解析 API v4 将科研 PDF 转为本地 Markdown、JSON 和图片，再交给 Codex 进行阅读、综述或审稿。显式使用 `vlm`，不调用 Agent 轻量解析接口。

## 功能

- 接收本地 PDF 或直接 HTTPS PDF 链接。
- 按文件内容与解析参数共享缓存，避免跨项目重复解析。
- 保存任务状态，支持续查和结果完整性校验。
- 为各项目维护独立文献索引，正文、结构化结果和图片保持不变。
- 在 ZIP 原文哈希完全一致时清理重复 PDF，并支持按缓存 ID 离线恢复原文。
- 为每次运行保存脱敏的结构化诊断日志，记录错误阶段、HTTP 状态和远端错误字段。
- 失败时明确报告，不静默降级为普通文本提取。
- Windows 支持本机 DPAPI 加密保存 Token；其他平台可通过环境变量提供 Token。

标题、DOI 或论文介绍页由 Codex 先定位可合法获取的 PDF；脚本本身只接收 PDF 文件或直接链接。解析会将 PDF 上传至 MinerU，需要用户授权；请遵守本地处理或保密要求。

## 安装

需要 Python 3.10+。

```sh
git clone https://github.com/whywhybe/codex-mineru-paper-reader.git
cd codex-mineru-paper-reader
python -m pip install -r requirements.txt
```

将仓库中的 `skill/` 整个文件夹复制到你的 Codex skills 目录，并重命名为 `codex-mineru-paper-reader`。如果配置了 `CODEX_HOME`，安装目标为该目录下的 `skills/codex-mineru-paper-reader`；否则使用自己的 Codex 配置目录。文件夹内应直接包含 `SKILL.md`、`scripts/` 和 `references/`。更新已有安装前先备份。

Windows 用户在自己的 PowerShell 窗口运行安装目录内的 `scripts/configure-token.ps1`，按提示输入 Token。不要把 Token 发到聊天、写入命令参数或提交到仓库。非 Windows 环境通过本机环境或秘密管理工具提供 `MINERU_API_TOKEN`。

## 使用

在 Codex 中要求使用 `codex-mineru-paper-reader` 阅读论文，或直接运行：

```sh
python skill/scripts/reader.py "/path/to/paper.pdf" --project "/path/to/research-project" --title "Paper title"
```

中文文献可加 `--language ch`，需要 OCR 时加 `--ocr`。返回 `pending` 后重复同一命令继续查询；有上传状态不确定等错误时先按操作说明恢复，不盲目重复提交。

没有本机配置时，默认缓存为用户主目录下的 `Documents/Codex/MinerU-Library`。可通过 `MINERU_CACHE_ROOT` 或 `--cache-root` 指定位置，也可在 `skill/config.json` 中配置 `cache_root`；该本机配置不纳入版本控制。优先级为命令行参数、环境变量、本机配置、默认目录。

项目的 `literature/index.json` 保存缓存标识；阅读笔记和实际阅读范围保存到项目的 `literature/notes/`。不要直接修改原始缓存，删除项目也不应连带删除共享缓存。

## 原文保留与离线恢复

解析期间保留原文快照。完成后，只有结果 ZIP 内 PDF 与输入的 SHA-256 完全一致，才执行去重：

| 输入 | 保留方式 |
|---|---|
| 本地 PDF | 用户原文件保持不变，记录其路径和哈希，删除缓存中相同的独立原文；ZIP 作为备份 |
| 网络 PDF | 保留一份经核验的解压原文，删除外层 `source.pdf` 及多余相同副本 |
| ZIP 没有原文或哈希不同 | 保留 `source.pdf`，不根据 `origin.pdf` 文件名判断等价 |

原本地文件已丢失、移动或改变时，可按缓存 ID 获取解析时的原始版本：

```sh
python skill/scripts/reader.py --original <cache_id> --cache-root "/path/to/cache"
```

命令返回可用的 `original_pdf` 路径，必要时从已核验 ZIP 恢复；不调用 API、不读取 Token、不覆盖用户当前文件。已完成的历史缓存不会批量清理。已有真实样例的 ZIP 原文与上传文件字节不同，因此保留原文，不能保证所有解析结果都能去重。

## 失败诊断与恢复

日志位于 `<cache-root>/logs/<cache_id>/<run_id>.jsonl`。输入尚未识别时位于 `logs/unassigned/`。命令输出和 manifest 会给出日志位置，manifest 同时保留 `last_error`、`remote_error` 和历次日志指针。

日志记录阶段、UTC 时间、耗时、状态变化、缓存/批次/尝试 ID、可用的 HTTP 状态和错误字段，以及输出清单。采用字段白名单和脱敏，不保存 Token、解密输出、请求头、签名 URL 或完整 API 响应。API 没有返回错误码时记录 `null`。

- **本机凭据访问或 DPAPI 解密失败**：与 API 鉴权拒绝分开报告。检查执行用户上下文、权限和凭据访问条件。此次改进增强了诊断并使 PowerShell 错误可靠终止；没有修复所有沙盒身份下的 DPAPI 解密问题，也不自动提升执行权限。
- **API 鉴权拒绝**：实际 401/403 或相应 API 鉴权错误码才提示检查、更换凭据。
- **限流、服务端错误或任务 failed**：保存观察到的状态和错误消息；重试成功不等于原始内部根因已确定。
- **提交或上传状态不明**：保留现有任务；`--recover` 只查询已知批次，不自动重复 POST/PUT。没有批次 ID 时需人工核对。
- **结果包下载或解压失败**：保留部分下载和旧 ZIP。显式 `--redownload-result` 会先保存旧包再下载同一任务的结果，不重新上传论文。

确认任务 failed 后，先离线归档旧尝试，再显式关联重试：

```sh
python skill/scripts/reader.py --archive-failed <cache_id> --cache-root "/path/to/cache"
python skill/scripts/reader.py "/path/to/paper.pdf" --language ch --cache-root "/path/to/cache" --retry-from <返回的归档目录名>
```

重试必须使用与旧尝试相同的输入和解析参数。旧失败缓存、manifest 和日志保留，新尝试记录旧批次及 manifest 哈希。详细分类和操作见 [诊断与恢复说明](skill/references/diagnostics.md)。

## 工作流范围

技能负责 Codex 自己选择的 PDF 读取阶段，可与普通对话、PDF 技能及 ARS 工作流衔接，但不会替换远端插件内部的解析器。保留后续工作流的来源、页码定位及引用核验要求。解析完成不代表已经阅读全文，也不证明公式、表格或结论正确。

如果希望所有科研 PDF 默认使用此流程，可以按需在自己的工作规则中指定：实质阅读科研 PDF 前先使用本技能，失败时报告原因，不静默降级。安装本仓库不会自动改写全局规则。

## Token 与提醒

不执行定期或调用前的到期检查，不额外调用 API 探测 Token；实际鉴权失败才提示更换，已有缓存仍可读取。

Windows 配置脚本可准备到期前一天 09:00（Asia/Shanghai）的单次提醒请求，但不会实际创建定时任务。提醒需用户授权及可用的调度工具；没有工具时保持待创建状态。详见 [操作说明](skill/references/operations.md)。

## 验证与已知边界

```sh
python -m unittest discover -s skill/scripts -p "test_*.py"
```

2026-10-02：47 项离线测试通过，技能格式校验通过，覆盖原文保留与恢复、错误分类、日志脱敏、不明状态恢复、失败尝试关联和 ZIP 证据保留。此前官方样例真实解析和跨项目缓存复用已通过；本次更新仅做离线验证，没有再次调用真实 API。

普通对话、官方 PDF 技能和 ARS 的三条路径尚未全部完成端到端验证。页码映射和科学内容准确率未验证。DPAPI 凭据设置仅适用于 Windows；未声称其他操作系统已完成端到端验收。

源码及操作约定见 [SKILL.md](skill/SKILL.md)。本仓库不包含私人聊天、论文缓存、本机配置或凭据。
