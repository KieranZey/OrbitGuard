# OrbitGuard 面试演示 + GitHub CI 实操教程

> 本教程的两部分内容均已**实测验证**（输出截图取自真实运行）。
> 目标：30 分钟内把三条 demo 跑顺，30 分钟内把仓库推上 GitHub 并看到 CI 全绿。

---

## 第一部分：亲手跑三条 demo（约 10 分钟）

### 0. 准备（一次性，30 秒）

```powershell
cd C:\Users\BLACKYZ\Desktop\OrbitGuard   # 进入项目目录
python --version                          # 需 ≥ 3.10，实测 3.13 可跑
chcp 65001                                # Windows 控制台中文乱码时先执行这行
```

> 提示：运行时会刷出大量 `[EVENT]` 行——**那不是报错，是结构化事件流**（heartbeat/telemetry/tool_call…），
> 正是你在面试里要讲的"事件契约"。只想看最终报告可以看输出末尾，或用 `--json` 参数。

### Demo 0（打底）：正常场景，先跑这条

```powershell
python five_module_orchestrator.py
```

**预期（约 0.5 秒跑完）**：

```
【RSM 运行稳定性】  故障: False
【DTM 输出可信度】  信任分: 1.0  风险: low
【Gateway 汇合判定】路由: PERMIT
【执行前置门控】    检查 0 次 / 放行 0 次 / 拦截 0 次
【最终结果】 EXECUTED (DELAY_TASK)
```

**讲解词**："这是全链路健康态：无故障 → 信任分 1.0 → C1–C5 全部通过 → PERMIT。
正常任务只调用只读工具（读遥测），不经过前置门控——这是许可经济性设计，只读操作不需要许可。"

---

### Demo 1（重点）：`--fault wrong_diagnosis` → 看 TOOL_GATED

```powershell
python five_module_orchestrator.py --fault wrong_diagnosis
```

**预期输出（实测，约 0.5 秒）**：

```
【RSM 运行稳定性】  故障: False
【DTM 输出可信度】  信任分: 0.7  风险: medium  问题: 1
【Gateway 汇合判定】路由: SUSPEND  遏制: PLANNING_BLOCK
【EGM 执行门控】    审批状态: PERMIT_ISSUED  执行状态: COMPLETED  动作: REJECT_TASK
【执行前置门控】    检查 1 次 / 放行 0 次 / 拦截 1 次
   拦截: SUSPEND -> Gateway 判定 SUSPEND（需人工审批）…自动模式拒绝放行…
                    DTM检测到 1 个可信度问题 (trust_score=0.70)
【最终结果】 TOOL_GATED (SUSPEND): …
```

**讲解词（面试官站在旁边时按这个顺序说）**：

1. "注入器把 Agent 的诊断结论篡改成了 `SA_BRANCH_SHORT`——太阳阵支路短路。"
2. "但 DTM 的结论一致性检查发现**遥测证据不支持**：`sa_current` 只有 4.5A，规则要求 >5A 才成立 → 扣 0.3 → 信任分 0.70 < 0.8 → SUSPEND。"
3. "Agent 根据（被篡改的）结论规划了处置动作：切换太阳阵支路——这是一个 **mutation 工具**。"
4. "**关键点：工具在执行前必须向 Gate 请求许可**。Gate 用'此刻的监护状态 + 动作意图'做裁决，SUSPEND 路由自动模式一律拒绝 → 工具从未执行，返回 GATED_DENIED。"
5. "所以最终结果是 TOOL_GATED——**拦截发生在执行之前，不是事后补审**。"

**如果面试官追问"你怎么证明工具真没执行？"** → 指输出里 `检查 1 / 拦截 1`；再看审计日志：

```powershell
Get-Content logs\five_mod_egm_audit.jsonl | Select-String GATE_DENIED | Select-Object -Last 1
```

---

### Demo 2：`--fault process_crash` → 看 BLOCK→SAFE_HOLD

```powershell
python five_module_orchestrator.py --fault process_crash
```

**预期输出（实测，约 2 秒）**：

```
【RSM 运行稳定性】  故障: True  类型: PROCESS_CRASH  严重度: critical
【DTM 输出可信度】  信任分: 1.0
【Gateway 汇合判定】路由: BLOCK  遏制: ISOLATE
【EGM 执行门控】    审批状态: SAFE_HOLD
【执行前置门控】    检查 0 次 / 放行 0 次 / 拦截 0 次
【最终结果】 SAFE_HOLD (Gateway BLOCK)
```

**讲解词**：

1. "Agent 在工具调用处注入崩溃（事件流里能看到 `agent_output status=failed`）。"
2. "**谁来检测？** 独立看门狗线程 + 墙钟：心跳超 1 秒没来 → PROCESS_CRASH(critical)。不依赖事件流——Agent 崩了以后没有新事件，事件驱动监护会死等，这是我踩过的坑。"
3. "critical → BLOCK → ISOLATE → EGM 直达 SAFE_HOLD，**BLOCK 不需要人工审批**。"
4. "注意 shutdown 事件带原因语义：正常停机发 `normal_shutdown` 看门狗忽略；异常路径发 `agent_fault`，看门狗继续判崩溃——否则'正常停机'会被误报成'崩溃'。"

---

### Demo 3：`--fault no_output_timeout` → 看独立判别

```powershell
python five_module_orchestrator.py --fault no_output_timeout
```

**预期输出（实测，⚠️ 会等约 6.5 秒——这是设计，不是卡死）**：

```
【RSM 运行稳定性】  故障: True  类型: NO_OUTPUT_TIMEOUT  严重度: medium
【DTM 输出可信度】  信任分: 1.0
【Gateway 汇合判定】路由: SUSPEND  遏制: TOOL_RESTRICT
【EGM 执行门控】    执行状态: COMPLETED  动作: REJECT_TASK
【最终结果】 EXECUTED (REJECT_TASK)
```

**讲解词**：

1. "注入器让工具调用 sleep 不返回（演示配置 6 秒）。"
2. "编排器有 5 秒超时兜底——所以这 6.5 秒 = 5s 超时保护 + 1.5s 看门狗判定窗口，**链路不会被一个挂死的工具拖垮**。"
3. "看门狗发现 tool_call 超过 1 秒没有 tool_result，同时**进程 CPU 占用≈0（阻塞特征）**→ 判 NO_OUTPUT_TIMEOUT，而不是死循环。"
4. "**对比试验**（可选现场加跑）：`python five_module_orchestrator.py --fault infinite_loop` ——同样挂起，但 CPU≈100%（忙等特征）→ 判 INFINITE_LOOP。**用 CPU 签名把两类挂起独立判别开**。"

> 面试官问"CPU 不明显怎么办"→ 答："阈值可配置；第二判据是执行方回报超时（tool_result 含 timed out）。判别不是单点的。"

---

### 九条故障速查表（供快速翻阅）

| 命令（`python five_module_orchestrator.py`） | 检出 | 路由 | 最终结果 | 耗时 |
|---|---|---|---|---|
| `--fault wrong_diagnosis` | DTM 0.70 | SUSPEND | **TOOL_GATED** | 0.5s |
| `--fault inflated_confidence` | DTM 0.50 | SUSPEND | **TOOL_GATED** | 0.5s |
| `--fault missing_evidence` | DTM 0.25 | BLOCK | SAFE_HOLD | 0.8s |
| `--fault contradictory_sensors` | DTM 冲突 | SUSPEND | 拦截/挂起 | 0.5s |
| `--fault process_crash` | RSM critical | BLOCK | **SAFE_HOLD** | 2s |
| `--fault infinite_loop` | RSM 忙等 | SUSPEND | REJECT_TASK | 6.5s |
| `--fault no_output_timeout` | RSM 阻塞 | SUSPEND | REJECT_TASK | 6.5s |
| `--fault memory_bloat` | RSM 内存 | SUSPEND | REJECT_TASK | 1s |
| `--fault repetitive_calls` | RSM 重复 | SUSPEND | REJECT_TASK | 0.8s |
| （无参数） | — | **PERMIT** | EXECUTED | 0.5s |

---

## 第二部分：推到 GitHub，让 CI 跑绿（约 30 分钟）

### 步骤 1：建 GitHub 仓库（网页，2 分钟）

1. 打开 https://github.com/new
2. Repository name 填 `OrbitGuard`，Public 或 Private 随意
3. **不要勾选** "Add a README / .gitignore / license"（要一个**空仓库**，否则推送会冲突）
4. Create repository，记下地址：`https://github.com/<你的用户名>/OrbitGuard.git`

### 步骤 2：本地关联远程并推送（2 分钟）

```powershell
cd C:\Users\BLACKYZ\Desktop\OrbitGuard
git remote add origin https://github.com/<你的用户名>/OrbitGuard.git
git branch -M main
git push -u origin main
```

**认证（三选一）：**

| 方式 | 适用 | 操作 |
|---|---|---|
| A. 浏览器登录弹窗（推荐） | 电脑上登录过 github.com | push 时 Windows 自动弹"Git Credential Manager"登录框，走网页授权 |
| B. 个人访问令牌 | 弹窗出不来 / 开了 2FA | GitHub → Settings → Developer settings → Personal access tokens → Tokens(classic) → Generate → 勾 `repo` → 复制；push 时**用户名填 GitHub 账号、密码填 token**（不是登录密码） |
| C. SSH | 已配过 SSH 密钥 | `git remote add origin git@github.com:<用户名>/OrbitGuard.git` |

> 注意：token 只贴进凭据弹窗，**不要写进任何会被提交的文件**。若网络直连 GitHub 慢，可先配代理：`git config --global http.proxy http://127.0.0.1:<端口>`。

### 步骤 3：看 CI 自动跑（3–6 分钟）

1. 推送成功即触发：仓库页 → **Actions** 标签 → 出现 "OrbitGuard CI"
2. 预期结构：
   - `unit-tests`：**4 个矩阵 job**（ubuntu/windows × Python 3.10/3.12）各自跑 8 个测试套件
   - `benchmark`：依赖 unit-tests 全绿后自动跑，产出量化实验报告
3. 全部变 **绿色勾** = "CI 全绿"达成
4. benchmark 产物：点进 benchmark job → 底部 **Artifacts** → 下载 `benchmark-report`（含报告 txt/json）

### 步骤 4：给 README 挂状态徽章（可选，30 秒）

Actions → 选 OrbitGuard CI workflow → 右上 **⋯ → Create status badge** → 复制 markdown → 粘到 README 顶部 → commit 推送。面试时说"README 上的徽章是真实 CI 状态，不是贴图"。

### 如果红了怎么办

1. 点进失败 job → 展开 "Run test suites" 步骤 → 找 FAIL 行与报错
2. **本地复现**：`python tests\<对应文件>.py`
3. 修完 `git commit -am "..."` + `git push`，CI 自动重跑
4. 常见坑提前说：Windows runner 下测试全过（已在本机 windows 实测 149/149）；若 ubuntu 上有平台差异，通常是路径/编码类小问题

---

## 附：演示完记得说的三句话

1. "这四条路径（PERMIT / TOOL_GATED / SAFE_HOLD / 独立判别）全部有自动化测试兜底：149 项单测 + benchmark 9/9。"
2. "所有数字都在仓库里可复现：`python experiments/run_benchmark.py` 一条命令出报告。"
3. "CI 里跑的和我本地跑的，是同一套测试脚本。"
