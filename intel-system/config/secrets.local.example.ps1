# 本地密钥（模板）——复制为 secrets.local.ps1 后填写
#
# 这个文件**不会被 run_scheduled.ps1 自动加载**，只有改名后才会。
# 目的：让密钥与代码分离，误提交时 .gitignore 能挡住。
#
# 用法：
#   copy config\secrets.local.example.ps1 config\secrets.local.ps1
#   然后填入真实值

# 告警 Webhook（企业微信 / 钉钉 / 飞书 / Slack）
# 留空则读取 config\alerts.yaml 里的配置
$env:ALERT_WEBHOOK_URL = ''

# 若要临时覆盖 LLM 密钥（优先级高于 config\llm.local.yaml）
# $env:OPENAI_API_KEY  = 'sk-...'
# $env:OPENAI_BASE_URL = 'https://api.deepseek.com/v1'
# $env:OPENAI_MODEL    = 'deepseek-chat'
