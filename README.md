# Campus Notice Bot（校园通知收集机器人）

> 本仓库的完整英文安装指南与组件清单位于各平台子目录内，本文件为根目录中文导览与快速入口。

**Campus Notice Bot**（v1.0.0，GPL-3.0）是一个面向中文高校 QQ 群的本地校园通知收集与整理工具，旨在减少学生每天手动翻看多个群聊漏掉通知的负担。

它通过 **NapCat（OneBot v11 WebSocket）** 静默监听你指定的 QQ 群，结合关键词筛选、可选的图片 OCR 和你自备的 LLM API，自动提取考试、截止日期、竞赛、时间与地点等行动项，并在本机提供**中文界面**的日历页面、汇总报告与 AI 问答。

---

## 核心特性

- **只读监听，不发送消息**：仅记录你在 `capture/groups.json` 中配置的白名单 QQ 群消息（其余群消息入库前直接丢弃），内置 QQ 回复适配器默认关闭且不外发消息；支持设置 `priority_members` 优先关注老师、辅导员或群管理员消息。
- **断线自动补拉**：记录捕获覆盖时间戳（`capture/coverage.json`），电脑关机或重连后自动回补遗漏的群历史消息，也支持手动运行 `capture/backfill.py --days 7` 补拉。
- **可选图片 OCR**：集成 Tesseract OCR（`chi_sim+eng`），识别群内截图/海报中的通知文字。
- **AI 结构化提取（BYOK）**：调用你自己的 OpenAI 兼容 Chat API（默认 `deepseek-chat` / `https://api.deepseek.com`，可在 `config.json` 中更换模型与地址）；发送前自动去除手机号与邮箱。
- **一体化本地 Web 界面**：单一本地程序 `tools/talk.py` 监听 `http://127.0.0.1:8790`（仅限本机访问）：
  - 日历视图：`/r/calendar.html`（支持 `.ics` 日历导出、点击 `✔ Mark done` 标记已完成）
  - 按群汇总：`/r/latest.html`、周报 `/r/weekly-<year>-W<nn>.html`
  - 站内/终端对话：页面右下角 **Ask the bot**、`/chat` 或 `cnb talk --terminal`（支持 `--no-llm` 离线仅引用本地数据）
- **多高校零代码适配**：通过修改 JSON 即可接入自己的学校：
  - `research_sources.json`：校内通知网站列表（强制遵守 `robots.txt`、2 秒请求间隔、仅限白名单域名；遇到反爬/验证码/登录页绝不绕过，而是转入 `Pages I could not open` 队列交由用户手动打开后自行粘贴/导入文本）；附带可选 `tunnel/relay.py` 处理纯 IPv4/IPv6 或中转访问。
  - `contest_whitelist.json`：学校/学院认可的竞赛白名单（需人工核对后执行 `cnb wl-confirm` 确认，未确认前不会直接标为已认定）。
  - `capture/keywords.json`、`interests.json`、`filters.json`：自定义关键词、校园教学楼/地点、关注主题与屏蔽规则。

---

## 仓库目录与平台选择

| 目录 | 适用平台 | 运行方式 | 详细文档（英文原版） |
|---|---|---|---|
| `campus-notice-bot-linux/campus-notice-bot-1.0.0-linux/` | **Linux / WSL2**（需开启 systemd） | Docker（NapCat）+ `systemd --user` | [安装与使用指南](campus-notice-bot-linux/campus-notice-bot-1.0.0-linux/README-INSTALL-LINUX.md) · [组件清单](campus-notice-bot-linux/campus-notice-bot-1.0.0-linux/COMPONENTS.md) |
| `campus-notice-bot/` | **Windows 10/11 原生**（无需 Docker / WSL） | 本地 Python + 自备 NapCat + Task Scheduler | [安装与使用指南](campus-notice-bot/README-INSTALL-WINDOWS.md) · [组件清单](campus-notice-bot/COMPONENTS.md) |
| `LICENSE` | — | GNU General Public License v3.0 | [LICENSE](LICENSE) |

> **Windows 原生版状态说明**：作者在文档中明确说明，Windows 原生版是在 Linux 下模拟 Windows 调用编写和测试的，**尚未在真实 Windows 电脑上完整实测**。使用前建议先双击运行 `campus-notice-bot/tools/wincheck.cmd`（只读自检，不启动任何服务）并查看生成的 `data/wincheck.txt`。

---

## 环境要求

- **Python**：作者运行环境为 Python `3.14.9`（代码依赖标准库 `zoneinfo`，至少需要 Python 3.9+；更低版本与 `requirements.txt` 固定版本的兼容性未经完整测试）。
- **LLM API Key**：需自行准备（默认 DeepSeek，可替换为任意 OpenAI 兼容服务）。
- **QQ 账号与手机 QQ**：仅用于扫码登录监听。
- **NapCat（QQ 桥接）**：
  - **Linux / WSL2**：需要 Docker Engine（含 `docker compose` v2），由安装器按固定 digest 拉取 `mlikiowa/napcat-docker` 镜像。
  - **Windows 原生**：因 NapCatQQ 官方仓库与文档站截至 2026-11-15 已停止发布，**安装器不会自动下载 NapCat**，需用户自行准备目录或压缩包并核对 SHA-256 后通过 `--napcat-dir` 或 `--napcat-zip --sha256` 传入。
- **Tesseract OCR（可选）**：需简体中文语言包 `chi_sim`（不安装也可运行，仅跳过图片文字识别）。

---

## 快速上手概览

完整安装命令、排错表与进阶配置请直接查阅对应平台的英文指南，以下为通用流程概览：

### 1. 安装依赖与注册后台任务

- **Linux / WSL2**（在 `campus-notice-bot-linux/campus-notice-bot-1.0.0-linux/` 下执行）：
  ```bash
  ./install.sh --dry-run   # 仅预览，不做任何修改
  sh install.sh            # 创建 .venv、生成配置模板、准备容器与 systemd 用户服务
  ```
- **Windows 原生**（在 `campus-notice-bot/` 下执行）：
  ```cmd
  tools\wincheck.cmd
  install.cmd --dry-run
  python install.py --napcat-dir C:\path\to\NapCat
  ```
  （也可先不带 NapCat 安装以测试报告与对话界面，后续再补充参数重跑安装器；如需传入压缩包请使用 `--napcat-zip <路径> --sha256 <哈希>`。）

### 2. 配置并验证 LLM API Key

```bash
# Linux 使用 ./cnb.sh，Windows 使用 cnb（即 cnb.cmd）
cnb key set        # 安全录入 API Key（写入 stack/.env，不回显）
cnb key test       # 发送一次轻量测试请求验证连通性
cnb setup          # 交互式初始化模型、时区、学校与基础选项
```

### 3. 扫码登录 QQ 并启用守护

```bash
# Linux: .venv/bin/python stack/bridge_guard.py --qr
# Windows: .venv\Scripts\python stack\bridge_guard.py --qr
```

手机 QQ 扫码确认后（二维码保存在 `stack/qrcode.png`，WebUI 位于 `http://127.0.0.1:6099`），确认 `--status` 返回 `"state": "online"`，再启用守护任务：

```bash
python install.py --enable-guard
```

### 4. 配置监听群号并打开报告

编辑 `capture/groups.json`，将其中的示例假群号替换为你的真实 QQ 群号（修改后每条消息自动重载，无需重启）：

```json
{"groups": [123456789, 987654321], "priority_members": [], "priority_by_group": {}, "priority_roles": [], "ignore_users": []}
```

在浏览器打开 `http://127.0.0.1:8790/r/calendar.html`（或运行 `./open-report.sh` / `open-report.cmd`）即可查看日历报告与使用内置对话；如需立即执行一次提取与报告生成，运行 `cnb daily`。

---

## 隐私、风控与部署红线（用户与 AI 助手配置必读）

> 如果你直接将本仓库交给 AI 编程助手协助部署，请务必遵守以下红线，并优先阅读目标平台子目录下的 `README-INSTALL-*.md` 与 `COMPONENTS.md`。

1. **严禁手动启动或重启 NapCat**：
   - 不要双击 NapCat 的 `.bat`，不要在任务计划程序/终端直接拉起，Linux 下不要手动执行 `docker start / restart / compose up` 且保持 `restart: "no"`。
   - 必须且只能通过 `stack/bridge_guard.py` 启动 NapCat，它内置了 **24 小时最多 6 次登录**上限与指数退避机制；高频重启会触发 QQ 会话失效。
   - 状态说明：`online` 正常｜`waiting` 退避等待中勿操作｜`needs_qr` 需手动执行一次 `--qr` 扫码｜`capped` 24 小时登录次数已满，等待并确认手机 QQ 状态后再按文档处理。
2. **账号风控与合规提示**：
   - 自动化 QQ 客户端不符合腾讯用户协议，存在账号受限风险，请自行评估并使用可接受风险的账号。
   - 本项目只读不发消息，不会向任何人索要或保存 QQ 密码、验证码。
3. **隐私与端口安全**：
   - 消息、SQLite 数据库、报告与登录态均保存在本机；仅脱敏手机号与邮箱后的文本会发往用户自备的 LLM API。
   - 端口 `3001`（OneBot WS）、`6099`（NapCat WebUI）、`8790`（报告与对话）均仅绑定 `127.0.0.1`，**切勿暴露到局域网或公网**。
   - 切勿向他人分享或提交 `stack/.env`、`stack/data`（Linux）或 `napcat/`（Windows）目录。
4. **先 `--dry-run`，不绕过反爬**：
   - 安装与变更配置前先执行 `--dry-run` 检查；对需要验证码或登录的校内网站，严禁尝试自动绕过，请使用内置的手动提供页面流程（`cnb page`）。

---

## 开源协议

本项目基于 [GNU General Public License v3.0](LICENSE) 开源。
