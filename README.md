# AIHOT 飞书每日推送

这个项目每天北京时间 08:00 通过 GitHub Actions 自动运行，读取 `config.yaml` 中的规则，从 AIHOT API 拉取 AI 行业资讯，完成初筛、OpenAI 评分、最终排序，并把 Top 5 新闻推送到飞书群。

## 文件说明

- `config.yaml`：业务配置、筛选规则、权重、模型配置、`scoring_prompt` 和 `ranking_prompt`。
- `.github/workflows/aihot-feishu.yml`：GitHub Actions 定时任务，支持手动触发。
- `scripts/push_aihot_feishu.py`：主脚本，读取配置并执行抓取、筛选、评分、排序和推送。
- `requirements.txt`：Python 依赖。

## GitHub Secrets

在仓库的 `Settings -> Secrets and variables -> Actions` 中添加：

- `OPENAI_API_KEY`：OpenAI API Key。
- `FEISHU_WEBHOOK`：飞书自定义机器人 webhook 地址。

## 运行时间

GitHub Actions 的 cron 使用 UTC 时间：

```yaml
cron: "0 0 * * *"
```

这对应北京时间每天 08:00。

## 本地调试

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="你的 OpenAI API Key"
export FEISHU_WEBHOOK="你的飞书机器人 webhook"
python scripts/push_aihot_feishu.py
```

Windows PowerShell：

```powershell
$env:OPENAI_API_KEY="你的 OpenAI API Key"
$env:FEISHU_WEBHOOK="你的飞书机器人 webhook"
python scripts/push_aihot_feishu.py
```

## 执行逻辑

1. 读取 `config.yaml`。
2. 请求 AIHOT API：`https://aihot.virxact.com/api/public/items`。
3. 请求参数来自配置：`mode=all`，`limit=100`。
4. 根据 `keep_keywords`、`exclude_keywords`、`valid_categories` 初筛。
5. 取 `ranking_pool_size` 条候选资讯。
6. 使用 OpenAI Responses API 和 `gpt-5.5` 对候选资讯逐条执行 `scoring_prompt`。
7. 根据 `weights` 计算 `final_score`。
8. 过滤低于 `relevance_threshold` 或 `final_score_threshold` 的资讯。
9. 使用 `ranking_prompt` 进行最终排序。
10. 选出 `target_top_n` 条新闻并推送到飞书群。

如果 AIHOT API 没有数据，或没有资讯通过筛选，脚本会推送：

```text
今日暂无可用AI行业新闻数据。
```

如果 OpenAI 或飞书推送失败，脚本会输出错误日志并退出。
