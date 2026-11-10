# ctxbench：日常任务模型的“实际可用上下文”测试

厂商标称的上下文是 100 万 token，不代表塞满 100 万还能好好干活。这个项目从很长的上下文开始，**逐档往下降**，
用一组日常难度的问题看模型在每一档还能不能答对，最后给出两个可以直接填进配置的数字：

- **推荐上下文**：这一档以及所有更短的档位，每次都满分。适合拿来做低难度、量大、不想出错的日常任务。
- **可用上限**：这一档以及所有更短的档位，平均分 ≥ 0.90 且单次最差 ≥ 0.80。超过它，模型开始明显漏信息。

所有请求都经由本机的 [opencode](https://opencode.ai) 发出，所以**你在 opencode 里能用的任何供应商和模型都能测**：
OpenCode Go / Zen 订阅、Anthropic、OpenAI、OpenRouter、自建的 OpenAI 兼容接口、本地 llama.cpp 等等。

## 榜单

<!-- LEADERBOARD:START -->
> 首轮测试还在跑，结果出来后会由 `python ctxbench.py report` 自动填到这里。
<!-- LEADERBOARD:END -->

## 测的是什么

每次请求是一份“工作记录归档”：几十万行彼此很像的服务日志、会议纪要、待办、聊天、资产登记、签到记录。
归档末尾问四类问题，模型只需输出一个 JSON：

| 题型 | 内容 | 考察点 | 权重 |
|---|---|---|---:|
| 检索 | 10 台设备的资产码，均匀埋在 5%–95% 深度 | 单点查找，看哪个深度开始丢 | 0.40 |
| 更新 | 4 台设备先登记、后被“更正通知”改码 | 能否取最新值而不是先看到的值 | 0.25 |
| 计数 | 某人的签到记录一共几条（5–9 条，散布全文） | 全文聚合 | 0.20 |
| 规则 | 归档第一行规定的答题规则 | 开头指令是否还被记得 | 0.15 |

- 干扰项与目标格式完全相同（同样有上百条资产登记和更正通知），不能靠“找唯一的那句话”取巧。
- 同一档位、同一次试验，所有模型拿到的题目内容相同（同一个随机种子）。
- 归档长度按各模型自己的分词器换算：先用 `calibrate` 实测每个模型的 token/字符 比例，所以“200K”对每个模型都约等于 20 万 token。
- 默认每个模型用它的**最高思考档**，测的是能力上限；想测日常实际用的档位，在 `config.json` 里改 `variant` 即可。

## 快速开始

需要 Python 3.9+（只用标准库）和已经登录好供应商的 opencode。

```bash
git clone https://github.com/shuiyue-cmyk/ctxbench.git
cd ctxbench
```

1. 编辑 `config.json`，写上要测的模型（格式与 `opencode models` 输出一致）：

   ```json
   {
     "rungs": [900000, 800000, 650000, 500000, 400000, 300000, 200000, 128000, 64000, 32000],
     "trials": 2,
     "concurrency": 0,
     "models": [
       { "model": "opencode-go/glm-5.3-flash", "variant": "max", "name": "GLM-5.3-Flash" },
       { "model": "openrouter/some-vendor/some-model", "variant": "high", "name": "Some Model", "max_context": 256000 }
     ]
   }
   ```

   - `variant`：opencode 里的思考档（`low` / `high` / `max` / `xhigh` 等，因模型而异），不需要可省略。
   - `max_context`：可选，大于它的档位直接跳过（比如模型本身只有 256K）。
   - `rungs`：起点和档位随意定；没必要从 100 万开始，可以从标称上限的 90% 左右起步，给思考和输出留出空间。

2. 校准、跑测试、出报告：

   ```bash
   python ctxbench.py calibrate
   ```

   ```bash
   python ctxbench.py run
   ```

   ```bash
   python ctxbench.py report
   ```

`run` 每完成一次请求就追加一行到 `results/raw.jsonl`，中断后再运行会跳过已完成的格子；只想补测某个模型或档位：

```bash
python ctxbench.py run --models opencode-go/glm-5.3-flash --rungs 64000 32000 --trials 1
```

`report` 会生成 `results/report.md`（逐档明细）、`results/chart.svg`（折线图），并刷新本 README 的榜单区块。

## 它怎么调用 opencode

- 通过 `opencode api GET /api/info` 找到 opencode 后台服务的地址，然后直接向服务的 `POST /api/experimental/generate/text` 发送整份归档。这是一次无状态的生成，不带 agent 的系统提示和工具，不会写入会话列表。
- 服务的密码从 `~/.config/opencode/service.json` 读取；也可以用环境变量 `OPENCODE_SERVER_URL`、`OPENCODE_SERVER_PASSWORD` 指定别的服务。
- 找 opencode 可执行文件的顺序：`OPENCODE_CLI` 环境变量 → `PATH` 里的 `opencode` / `opencode-cli` → Windows 桌面版自带的 CLI。
- 校准用的是 `opencode run --format json` 返回的输入 token 数（短消息与长样本之差 ÷ 字符数）。
- 所有（模型 × 档位 × 次数）的格子放进同一个线程池并发发出，长档位先发。并发数由 `config.json` 的 `concurrency` 控制，`0` 表示全部同时发出；供应商有并发或速率限制时调小即可。5xx、429、超时、空回答会等 30 秒自动重试，最多 3 次。

## 注意事项

- 每档默认只跑 2 次，单次结果有运气成分。看“可用上限”要结合逐档明细里的“最差一次”。
- 档位长度是按校准比例换算的，实际 token 数可能有百分之几的偏差。
- 结果反映的是“经由该供应商、在测试当天”的表现。同一个模型换供应商、换思考档，结果都可能不同。
- 题目是中文的工作记录；英文或代码为主的上下文，表现可能不一样。
