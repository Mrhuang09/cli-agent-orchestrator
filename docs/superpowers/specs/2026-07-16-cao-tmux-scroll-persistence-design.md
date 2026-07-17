# CAO tmux 滚动能力持久化设计

## 目标

所有由 CAO 创建的 tmux 会话默认支持鼠标滚轮进入 copy-mode，并为新建 pane 提供 20,000 行历史缓冲。该行为应随 `cli-agent-orchestrator` 安装包传播，使其他项目无需手工执行 `tmux set-option`。

## 现状与根因

CAO 通过默认 tmux server 创建会话，但未显式配置 `mouse` 与 `history-limit`。因此：

- `mouse` 继承 tmux 默认值 `off`，终端滚轮不能直接浏览历史；
- `history-limit` 继承默认值 2,000，长时间运行后可回看的输出有限；
- 当前人工执行的 `tmux set-option -g ...` 只对当前 tmux server 生命周期有效，不能随 CAO 安装传播。

## 方案

在 `TmuxClient.create_session` 创建首个 pane 之前，幂等配置 CAO 所使用的 tmux server：

- `mouse=on`；
- `history-limit=20000`。

随后按现有流程创建 220×50 的 detached session。配置放在源码而不是项目配置或用户 `~/.tmux.conf` 中，因此所有使用该 CAO 安装包的项目自动继承。

### 作用范围

CAO 当前使用用户默认 tmux server，因此这两个全局默认值也会影响同一 server 上之后新建的非 CAO session/pane。影响是可接受的：鼠标滚动属于交互增强；更高历史上限只增加有界内存使用，不改变 agent 输入、消息投递或会话身份。

不修改已经存在 pane 的历史容量；它们在下一次 CAO 重建后获得 20,000 行上限。`mouse=on` 对当前 server 可立即生效。

## 失败语义

tmux 选项配置属于会话可用性要求。若配置命令失败，`create_session` 应失败并沿用现有异常回滚路径，不静默创建一个行为不一致的会话。

重复创建不同 CAO 项目会话时重复设置相同值必须幂等。

## 测试

单元测试应证明：

1. 创建 session 前设置 `mouse=on` 与 `history-limit=20000`；
2. 选项配置失败时不调用 `new_session`，错误向上传播；
3. 原有 220×50、环境过滤及创建成功测试保持通过。

发布前运行 tmux client 定向测试、authority/inbox/status 相关回归测试，并运行仓库完整测试集。

## 发布

本设计及实现追加到现有 `fix/codex-inbox-idle-detection` 分支。验证通过后推送到 `Mrhuang09/cli-agent-orchestrator`，创建以 `main` 为目标的草稿 PR；不直接改远端 `main`。
