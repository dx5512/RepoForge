"""
chat_ops/feishu_cards.py - Feishu Interactive Card Builder
"""

import json

class FeishuCardBuilder:
    COLORS = {
        "pending": "blue",
        "planning": "purple",
        "running": "wathet",
        "reviewing": "orange",
        "success": "green",
        "failed": "red"
    }

    @staticmethod
    def build_task_card(task_id: int, target_repo: str, description: str, status: str, details: str, pr_url: str = None) -> dict:
        """
        构建动态更新的飞书消息卡片
        """
        color = FeishuCardBuilder.COLORS.get(status, "blue")
        repo_display = target_repo if target_repo else "本地沙盒项目"
        
        status_text = {
            "pending": "⏳ 任务排队中...",
            "planning": "🧠 架构师正在分析代码库并制定实施方案...",
            "running": "🏃‍♂️ Agent 正在编写代码...",
            "reviewing": "👀 Reviewer 正在审查代码...",
            "success": "✅ 研发完成，PR 已就绪！",
            "failed": "❌ 任务失败或被终止"
        }.get(status, "未知状态")

        elements = [
            {
                "tag": "markdown",
                "content": f"**🎯 目标仓库:** `{repo_display}`\n**📝 需求描述:**\n{description}"
            },
            {
                "tag": "hr"
            },
            {
                "tag": "markdown",
                "content": f"**📊 当前状态:** <font color='{color}'>{status_text}</font>\n\n**💡 详细日志:**\n{details}"
            }
        ]

        # 如果有 PR 链接，添加跳转按钮
        if pr_url:
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔗 点击查看 Pull Request"
                        },
                        "type": "primary",
                        "multi_url": {
                            "url": pr_url,
                            "pc_url": pr_url,
                            "android_url": pr_url,
                            "ios_url": pr_url
                        }
                    }
                ]
            })

        card = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": color,
                "title": {
                    "content": f"🤖 RepoForge 任务 #{task_id}",
                    "tag": "plain_text"
                }
            },
            "elements": elements
        }
        
        return card