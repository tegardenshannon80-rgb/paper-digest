# 搜广推顶会论文每日推送

OpenAlex 取数 -> 彩云小译翻译 -> 飞书机器人卡片，每天北京时间 08:00 由 GitHub Actions 运行。

- 主脚本：`feishu_digest.py`
- 状态：`data/digest_state.json`（记录已推送论文，避免重复）
- 修改参数：编辑工作流顶部的 `CONFS` / `LIMITS`
