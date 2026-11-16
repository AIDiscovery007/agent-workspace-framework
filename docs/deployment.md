# 部署手册

## 1. 环境与配置

基础框架：Python ≥3.11、Git、macOS 或 Linux。完整参考栈：macOS、Node ≥22、Codex CLI/App Server、已授权 Hi CLI、Hi 应用机器人；独立可见 session 需要 Herdr 的 `herdr-app` 与 `hc`。各部署使用自己的账号、应用、凭据和状态，一个机器人应用只由一套服务消费。

先在仓库创建独立 Python 环境并安装锁定依赖；后续命令使用这个环境。Pillow 用于图片解码和基础验收，与可选音视频依赖分开。安装器会记录当前解释器的绝对路径，正式部署后保留此环境位置。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
```

先运行已安装工具的 `--help`/`--version`。Codex 完整适配器要求 `--no-daemon`、App Server stdio、自定义文件权限、archive/delete 与可读取本轮元数据；缺功能停止该适配器，不通过移除沙箱解决。具体安装用各产品官方/组织认可的渠道。Hi CLI 和 Herdr 不作为公共 npm 依赖分发。

配置从 `config/workspace.example.json` 复制到本机（文件名用 `workspace.local.json`，已忽略）。必须替换真实 owner/model/Hi 群及 bot ID；设置 `herdr_space`、档案根 ID、启用任务和成员。`owner` 取 `hi search:me`，机器人/群 ID 从当前平台查询；Herdr 先读 `herdr-app --skill`、`hc --skill`、`herdr-app commands`，再 `herdr-app ls` 查询 space。

成员示例（全部为虚构数据，部署时替换）：

```json
{"account":"alice@example.test","slug":"alice","skills":[],"upload_folder":"Agent/成员产物/alice","browser":false}
```

启用网盘时安装官方 hi-pan Skill 到目标 `.agents/skills/hi-pan`；启用浏览器时需安装 agent-browser Skill，并设置 `hi.browser_domain`。二进制路径通过 `binaries` 覆盖自动发现。浏览器环境显式指定 `AW_BROWSER_BIN`（native binary）与 `AW_BROWSER_ENGINE_DIR`（含 chrome-headless-shell）；它们必须位于外层沙箱已批准可读路径并经过实际验证。Chromium 内层沙箱关闭的同时保留外层 macOS 文件沙箱，不支持无隔离回退。代理方法不等于浏览器站点的操作级授权，见权限文档。

`governance_full_access=true` 表示所有者明确选择让治理进程在精确治理任务范围内使用 full access/never。默认 false。守墓人不开发；该选项不扩大普通成员权限。`tasks` 默认全部 false；开启 organize 代表授权本工作区内按 SOP 移动、精确路径修改和字节相同副本去重。报告 session 使用永久删除的生命周期；开启日报/周报前确认接受，证据与回执保留。

## 2. 生成正式工作区

命令从本仓库执行；路径换为本次目标。core 与 full 不原地切换，前者可用临时试运行目录，正式目录使用已经填好的 full 配置。

```bash
python3 scripts/aw.py init --root /absolute/new-workspace --config config/workspace.local.json --profile full
python3 scripts/aw.py doctor --root /absolute/new-workspace --live
```

安装器拒绝覆盖非空已有目录；同配置重复调用不改状态。生成完只算安装完成，服务仍关闭。需要扩展到已有业务项目，按升级文档做审阅合并。

## 3. Hi 接入分层验收

以下 `<bridge>` 为目标 `.codex/hi-bridge` 的绝对路径。平台侧先确认机器人已发布、本人和机器人在目标群、`asn_bot_callback_msg` 事件以及读取消息/成员和 `im:message.sendMarkdownToChat:v1` 发送权限。实际 CLI/API help 是参数来源；不要全选无关权限。

```bash
python3 scripts/bridge_ctl.py --base <bridge> credentials
python3 scripts/bridge_ctl.py --base <bridge> deps
python3 scripts/bridge_ctl.py --base <bridge> doctor
python3 scripts/bridge_ctl.py --base <bridge> probe-ws --seconds 120
python3 scripts/bridge_ctl.py --base <bridge> test-send
python3 scripts/bridge_ctl.py --base <bridge> smoke-codex
python3 scripts/bridge_ctl.py --base <bridge> baseline
python3 scripts/bridge_ctl.py --base <bridge> service install
python3 scripts/bridge_ctl.py --base <bridge> status
```

credentials 在本机终端隐藏输入 appSecret/userAccessToken，0600 写入目标 `.secrets/hi.json`；不从原作者实例复制。Hi CLI 本人登录与应用凭据是两层授权。`probe-ws` 按输出标记让真人真实 @；`test-send` 向配置群发送唯一标记并回读；`smoke-codex` 在隔离状态下执行真实只读回合。缺真人步骤就保持 pending。只读探针线程和 turn 的沙箱均显式为 read-only。

冻结 baseline 后，在目标群发一条新的真实 @任务，包含 `HI-E2E-<本次唯一标记>` 并要求回复该标记。取得真实 messageId：

```bash
python3 scripts/bridge_ctl.py --base <bridge> accept --message-id <真实ID> --marker <完整标记>
```

accept 验证唯一任务、WS/轮询来源、模型结果、应用直发及原群回读。轮询测试可用 `--source any`，仍须另验 WS。不要伪造 proof 文件或把心跳当真人任务通过。

## 4. 定时与治理

先让 Herdr 当前 space 指向目标目录；核对真实模型和 effort 可用。报告根文档需本人有写权；通知启用时，在 `通知配置/webhooks.json` 的 `hi.codex_scheduled_task_complete.url` 填本人授权 Webhook（本机编辑，不回显）。交互回复始终用应用身份，定时通知单独走 Webhook，不作互相回退。

```bash
python3 scripts/aw.py service --root <工作区> --task organize generate
python3 scripts/aw.py service --root <工作区> --task organize install
python3 scripts/aw.py service --root <工作区> --task daily-log install
python3 scripts/aw.py service --root <工作区> --task weekly-report install
python3 scripts/aw.py service --root <工作区> --task governance install
```

仅执行已启用的模块。安装需要实际 doctor 与模型探针证据；治理还需要明确 full-access opt-in。调度使用间隔检查和配置时区门槛，不修改系统时区。周一至五不等于法定工作日，尚未接入节假日调休。可选 `--task keepawake install` 使用 caffeinate -i，允许息屏且阻止空闲睡眠，会耗电；不保证合盖、主动睡眠、关机或离线执行。

停止任务：`aw.py service --root <工作区> --task <名称> stop`；状态用 status。桥接停止/恢复使用 `bridge_ctl.py --base <bridge> service stop/start`，保留队列、游标与回执。治理停止调度不会强杀已运行独立 Agent。

## 5. 可选媒体

图片/附件路径已包含。音视频需要离线解码环境和模型：见 [媒体配置](media.md)。未准备这些资源时，音视频返回明确缺依赖错误；不能算完整媒体能力通过。浏览器和媒体均需针对目标机器实际做外层文件沙箱测试。
