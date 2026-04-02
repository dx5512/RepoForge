"""
Feishu Bot - ChatOps Interface for RepoForge

Integrates with the native SuperAgent core for sandboxed code task execution.
Now fully supports dynamic cross-repository code tasks via [repo: username/repo] tags.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import asyncio
import logging
import json
import threading
import subprocess
import uuid
import time
import re
import structlog
from typing import Optional
from dataclasses import dataclass
from enum import Enum
from concurrent.futures import ThreadPoolExecutor
from threading import Semaphore, Lock

import httpx
import lark_oapi as lark
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from chat_ops.feishu_cards import FeishuCardBuilder
from chat_ops.card_manager import get_card_manager, TaskCardSession
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, PatchMessageRequest, PatchMessageRequestBody
from utils.monitoring import setup_monitoring

from dotenv import load_dotenv
load_dotenv(override=True)

from config import get_config
from utils.retry import retry_network
from utils.logging_config import setup_logging
import structlog

config = get_config()

# Initialize structured logging
setup_logging(level=config.log_level)

logger = structlog.get_logger(__name__)

# Global shutdown flag
shutting_down = False


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SWETask:
    task_id: int
    user_id: str
    chat_id: str
    message_id: str
    description: str
    repo_path: Optional[str] = None
    target_repo: Optional[str] = None  # 💥 新增：动态接管任意远端 GitHub 仓库
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    git_diff: Optional[str] = None


class SWETaskManager:
    def __init__(self, workdir: Path, worktrees_base: Path):
        self.workdir = workdir
        self.worktrees_base = worktrees_base
        self.worktrees_base.mkdir(exist_ok=True)
        self.tasks: dict[int, SWETask] = {}
        self._next_task_id = 1
        self._lock = threading.Lock()  # 🔒 线程锁，保护 task_id 生成和任务字典访问

    def create_task(self, user_id: str, chat_id: str, message_id: str, description: str) -> SWETask:
        with self._lock:  # 原子操作：分配 ID 并注册任务
            task = SWETask(
                task_id=self._next_task_id,
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                description=description
            )
            self.tasks[self._next_task_id] = task
            self._next_task_id += 1
            return task

    def update_task(self, task_id: int, **kwargs) -> None:
        with self._lock:  # 保护任务字典的写入
            if task_id in self.tasks:
                for key, value in kwargs.items():
                    if hasattr(self.tasks[task_id], key):
                        setattr(self.tasks[task_id], key, value)

    def get_task(self, task_id: int) -> Optional[SWETask]:
        """线程安全地获取任务"""
        with self._lock:
            return self.tasks.get(task_id)


WORKDIR = Path(__file__).parent.parent

# 本地 Worktree 临时目录
WORKTREES_BASE = WORKDIR / ".feishu_worktrees"
WORKTREES_BASE.mkdir(exist_ok=True)

# 动态 Clone 临时目录 (阅后即焚)
DYNAMIC_REPOS_BASE = WORKDIR / ".dynamic_repos"
DYNAMIC_REPOS_BASE.mkdir(exist_ok=True)

task_manager = SWETaskManager(WORKDIR, WORKTREES_BASE)

SESSION_CACHE = {}
SESSION_CACHE_LOCK = threading.Lock()  # 🔒 保护 SESSION_CACHE 的全局锁

# 🔥 新增：并发控制 - 线程池 + 信号量
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))  # 默认5个并发
TASK_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_TASKS,
    thread_name_prefix='repoforge-task-'
)
# 使用信号量进行运行时流量控制（可选更精细的并发控制）
CONCURRENCY_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_TASKS)

# 从统一配置读取
FEISHU_APP_ID = config.feishu_app_id
FEISHU_APP_SECRET = config.feishu_app_secret


def send_text_message(chat_id: str, text: str):
    """保留这个为了兼容纯文本报错提示"""
    try:
        client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
            CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text").content(json.dumps({"text": text})).build()
        ).build()
        client.im.v1.message.create(request)
    except Exception as e:
        logger.error(f"Error sending text: {e}")

def send_interactive_card(chat_id: str, card_json: dict) -> Optional[str]:
    """发送卡片并返回 message_id，用于后续更新"""
    try:
        client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
            CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(json.dumps(card_json)).build()
        ).build()
        response = client.im.v1.message.create(request)
        if response.success():
            return response.data.message_id
        else:
            logger.error(f"Failed to send card: {response.code}, {response.msg}")
    except Exception as e:
        logger.error(f"Error sending card: {e}")
    return None

def update_interactive_card(message_id: str, card_json: dict):
    """根据 message_id 原地刷新卡片内容"""
    try:
        client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(
            PatchMessageRequestBody.builder().content(json.dumps(card_json)).build()
        ).build()
        client.im.v1.message.patch(request)
    except Exception as e:
        logger.error(f"Error updating card: {e}")


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    try:
        sender = data.event.sender
        message = data.event.message

        user_id = sender.sender_id.open_id
        chat_id = message.chat_id
        message_id = message.message_id
        content_obj = json.loads(message.content)
        
        text = ""
        if isinstance(content_obj, dict):
            if "text" in content_obj:
                text = content_obj["text"]
            else:
                parts = []
                for lang_data in content_obj.values():
                    if isinstance(lang_data, dict) and "content" in lang_data:
                        for line in lang_data["content"]:
                            for element in line:
                                if isinstance(element, dict) and "text" in element:
                                    parts.append(element["text"])
                text = "\n".join(parts)
        
        text = text.strip()
        if not text:
            return

        # Graceful shutdown: ignore new messages if shutting down
        if shutting_down:
            logger.info("Shutting down, ignoring new message")
            return

        logger.info(f"Received message from {user_id}: {text[:100]}...")

        # ==========================================
        # 💥 核心：拦截【提PR】和【重推】指令
        # ==========================================
        clean_text = text.lower()
        if clean_text in ["重新提交", "重试", "retry", "提pr", "提交pr", "push", "提交"]:
            with SESSION_CACHE_LOCK:  # 🔒 线程安全读取
                if chat_id in SESSION_CACHE:
                    cache = SESSION_CACHE[chat_id].copy()  # 复制一份，避免在异步线程中修改原数据
                else:
                    cache = None

            if cache:
                send_text_message(chat_id, "💪 收到指令！正在接管挂起的本地沙盒，尝试向 GitHub 推送并创建 PR...")

                def process_cached_pr():
                    is_dynamic = cache.get("is_dynamic", False)
                    wt_path = cache.get("wt_path")
                    is_retry = cache.get("is_retry", False)
                    branch_name = cache.get("branch_name")

                    pr_url = push_branch_and_pr(
                        cache["task"],
                        wt_path,
                        cache["git_diff"],
                        is_retry=is_retry,
                        retry_branch=branch_name,
                        is_dynamic=is_dynamic
                    )

                    if pr_url:
                        send_text_message(chat_id, f"✅ 代码已成功推送至远端！\n🔗 PR 链接: {pr_url}")
                        if is_dynamic and wt_path and Path(wt_path).exists():
                            from sandbox.worktree import robust_rmtree
                            try: robust_rmtree(str(wt_path), max_retries=3, logger=logger)
                            except: pass
                        with SESSION_CACHE_LOCK:  # 🔒 线程安全删除
                            SESSION_CACHE.pop(chat_id, None)
                    else:
                        send_text_message(chat_id, "❌ 推送失败，沙盒环境已保留。您可以稍后再次回复【重试】。")
                        cache["is_retry"] = True
                        cache["is_retry"] = True

                threading.Thread(target=process_cached_pr, daemon=True).start()
            else:
                send_text_message(chat_id, "⚠️ 当前没有挂起或待提交的沙盒任务。")
            return

        # ==========================================
        # 💥 核心：识别跨项目托管标签 [repo: dx5512/vue-app]
        # ==========================================
        target_repo = None
        repo_match = re.search(r"\[repo:\s*([^\]\s]+)", text, re.IGNORECASE)
        if repo_match:
            raw_repo = repo_match.group(1).strip()
            # 强力剔除飞书富文本自动带入的各种多余方括号
            target_repo = raw_repo.replace('[', '').replace(']', '')
            
            # 将 [repo: xxx] 从发给大模型的指令正文中清理干净
            text = re.sub(r"\[repo:\s*[^\]\s]+\]?", "", text, flags=re.IGNORECASE).strip()
            clean_text = text.lower()
            
        greetings = ["你好", "hello", "hi", "在吗", "ping", "测试", "test", "有人吗", "哈喽", "滴滴", "今天星期几"]
        if clean_text in greetings or (len(clean_text) < 8 and not any(k in clean_text for k in ["写", "修", "加", "代码", "bug"])):
            help_msg = "👋 你好！我是 RepoForge 自动代码工厂。\n\n🎯 跨项目接管格式示例：\n[repo: your-org/your-repo] 请在根目录新建一个 demo.py。"
            send_text_message(chat_id, help_msg)
            return

        task = task_manager.create_task(user_id, chat_id, message_id, text)
        task.target_repo = target_repo  # 注入动态仓库变量

        # 🔥 使用线程池提交任务，实现并发限制
        try:
            TASK_EXECUTOR.submit(process_swe_task_wrapper, task)
            logger.info(f"Task #{task.task_id} submitted to executor (active workers: {TASK_EXECUTOR._work_queue.qsize()})")
        except RuntimeError as e:
            # 线程池已关闭（程序正在退出）
            send_text_message(chat_id, "⚠️ 系统正在关闭，无法接受新任务。")
            logger.warning(f"Executor shutdown, rejecting task #{task.task_id}")

    except Exception as e:
        logger.error(f"Error handling message: {e}")


def process_swe_task_wrapper(task: SWETask):
    """
    Wrapper for process_swe_task to be used with ThreadPoolExecutor.
    Handles exceptions and logging at the thread pool level.
    """
    try:
        process_swe_task(task)
    except Exception as e:
        logger.error(f"Task #{task.task_id} failed in executor: {e}", exc_info=True)
        # 发送失败通知
        try:
            send_text_message(task.chat_id, f"❌ 任务执行失败（线程池级别）: {str(e)}")
        except:
            pass  # 避免二次异常


def process_swe_task(task: SWETask):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(execute_swe_task(task))


async def execute_swe_task(task: SWETask):
    from mcp_client import MCPClient
    from agent.super_agent import run_agent_task, extract_git_diff
    from agent.planner_agent import run_planner_task
    from utils.monitoring import tasks_started_total, active_tasks, task_duration_seconds, tasks_completed_total
    import time

    mcp = MCPClient(workdir=WORKDIR, worktrees_base=WORKTREES_BASE)

    task_manager.update_task(task.task_id, status=TaskStatus.RUNNING)

    # Monitoring: task start
    start_time = time.time()
    tasks_started_total.inc()
    active_tasks.inc()
    task_success = False

    is_dynamic = bool(task.target_repo)
    dynamic_clone_dir = None
    cleanup_needed = True
    card_msg_id = None

    try:
        # 🌟 使用 TaskCardSession 管理卡片生命周期
        card_session = TaskCardSession(
            task_id=task.task_id,
            chat_id=task.chat_id,
            target_repo=task.target_repo,
            description=task.description
        )

        # 发送初始卡片
        card_msg_id = card_session.send_new(
            send_func=send_interactive_card,
            status="pending",
            details="系统已接管任务，正在初始化环境..."
        )

        if is_dynamic:
            send_text_message(task.chat_id, f"🚀 收到任务！系统检测到 `[repo: {task.target_repo}]` 标签。\n🌐 正在从 GitHub 实时拉取最新仓库代码...")

            unique_id = uuid.uuid4().hex[:8]
            dynamic_clone_dir = DYNAMIC_REPOS_BASE / f"repo_{unique_id}"
            wt_path = dynamic_clone_dir

            github_token = config.github_token
            if not github_token:
                raise Exception("必须在 .env 中配置 GITHUB_TOKEN 才能使用跨项目动态克隆功能！")

            clone_url = f"https://oauth2:{github_token}@github.com/{task.target_repo}.git"

            import shutil

            max_clone_retries = 3
            clone_success = False
            last_err_msg = ""

            for attempt in range(max_clone_retries):
                try:
                    if dynamic_clone_dir.exists():
                        shutil.rmtree(dynamic_clone_dir, ignore_errors=True)

                    subprocess.run(["git", "clone", "--depth", "1", clone_url, str(dynamic_clone_dir)], check=True, capture_output=True)
                    clone_success = True
                    break
                except subprocess.CalledProcessError as e:
                    last_err_msg = e.stderr.decode('utf-8', errors='ignore').replace(github_token, '***')
                    if attempt < max_clone_retries - 1:
                        logger.warning(f"Git clone failed (Network error), attempt {attempt+1}/{max_clone_retries}. Retrying in 5 seconds...")
                        time.sleep(5)

            if not clone_success:
                raise Exception(f"克隆仓库失败。\n最终解析的仓库名: {task.target_repo}\n请检查网络代理或仓库名是否正确。\n日志: {last_err_msg}")

            subprocess.run(["git", "config", "user.name", "RepoForge"], cwd=wt_path, check=True)
            subprocess.run(["git", "config", "user.email", "agent@repoforge.local"], cwd=wt_path, check=True)
        else:
            send_text_message(task.chat_id, "🚀 收到本地研发任务！正在拉取沙盒隔离环境...")

            # 优先使用 TARGET_REPO_PATH 配置，否则回退到项目根目录
            repo_path_str = config.target_repo_path or str(WORKDIR)
            target_repo_path = Path(repo_path_str).resolve()
            task_manager.update_task(task.task_id, repo_path=str(target_repo_path))

            unique_id = uuid.uuid4().hex[:8]
            wt_name = f"feishu_{unique_id}"

            # Pre-clean any leftover worktree with same name
            try:
                mcp.worktree_remove(task.task_id, name=wt_name, keep_files=False, workdir=target_repo_path)
            except Exception:
                pass

            # Create worktree via MCP client
            mcp.worktree_create(task.task_id, wt_name, base_ref="HEAD", workdir=target_repo_path)
            try:
                from utils.monitoring import worktree_create_total
                worktree_create_total.inc()
            except Exception:
                pass

            # Compute worktree path (deterministic)
            wt_path = WORKTREES_BASE / f"{target_repo_path.name}_{wt_name}"

        if card_msg_id:
            card_session.update(
                update_func=update_interactive_card,
                status="running",
                details="代码仓库已就绪，正在启动沙盒容器..."
            )

        # Create sandbox container via MCP client
        mcp.sandbox_create_container(task.task_id, wt_path)
        try:
            from utils.monitoring import container_create_total
            container_create_total.inc()
        except Exception:
            pass

        if card_msg_id:
            card_session.update(
                update_func=update_interactive_card,
                status="running",
                details="🏃‍♂️ Agent 正在分析代码库并开始构建..."
            )

        enriched_prompt = f"""User Request: {task.description}

[SYSTEM DIRECTIVE: You are in a secure Docker Sandbox. The requested repository is mounted at '/workspace'. Ignore Windows paths (like D:\\). Do all work inside '/workspace'.]"""

        if card_msg_id:
            card_session.update(
                update_func=update_interactive_card,
                status="planning",
                details="🧠 架构师智能体正在探索代码库并生成 task_plan.md..."
            )

        logger.info(f"[Task #{task.task_id}] Starting Planner")
        planner_result = run_planner_task(task.description, task.task_id, mcp_client=mcp)
        logger.info(f"[Task #{task.task_id}] Planner completed, success={planner_result.get('success')}")

        # 检查 Planner 是否成功生成了 plan 文件
        plan_file_path = wt_path / "task_plan.md"
        if plan_file_path.exists():
            plan_content = plan_file_path.read_text(encoding='utf-8', errors='ignore')
            logger.info(f"[Task #{task.task_id}] Plan file generated: {len(plan_content)} characters")
            logger.debug(f"[Task #{task.task_id}] Plan preview: {plan_content[:300]}...")
        else:
            logger.error(f"[Task #{task.task_id}] Plan file NOT found after planner execution!")

        # 如果 Planner 失败或没有生成 plan，提前终止任务
        if not planner_result.get("success"):
            logger.error(f"[Task #{task.task_id}] Planner failed, aborting task")
            if card_msg_id:
                card_session.update(
                    update_func=update_interactive_card,
                    status="failed",
                    details=f"❌ 架构师智能体规划失败。请检查 API 配置或重试。\n\n错误: {planner_result.get('final_message', 'Unknown error')}"
                )
            raise Exception(f"Planner failed: {planner_result.get('final_message', 'Unknown error')}")

        if not plan_file_path.exists():
            logger.error(f"[Task #{task.task_id}] No plan file generated, aborting")
            if card_msg_id:
                card_session.update(
                    update_func=update_interactive_card,
                    status="failed",
                    details="❌ 架构师智能体未能生成实施计划。\n\n可能原因：\n- API 速率限制\n- 模型配置错误\n- 网络问题\n\n请检查配置后重试。"
                )
            raise Exception("Planner did not generate task_plan.md")

        mcp.bash_execute(task.task_id, "git config --global user.email 'agent@repoforge.local'")
        mcp.bash_execute(task.task_id, "git config --global user.name 'RepoForge'")
        mcp.bash_execute(task.task_id, "git add . && git commit -m 'chore: baseline snapshot before coder execution'")

        if card_msg_id:
            card_session.update(
                update_func=update_interactive_card,
                status="running",
                details="🏃‍♂️ Coder 正在读取 task_plan.md 并开始编写代码..."
            )

        from agent.reviewer_agent import review_code

        max_revisions = 3
        revision_count = 0
        current_prompt = enriched_prompt
        final_git_diff = ""
        is_approved = False
        critique_history = []

        while revision_count < max_revisions:
            result = run_agent_task(
                task_prompt=current_prompt,
                task_id=task.task_id,
                workspace_path=wt_path,
                max_iterations=15,
                mcp_client=mcp
            )
            iterations = result.get("iterations", 0)
            # Record agent iterations metric
            try:
                from utils.monitoring import agent_iterations
                agent_iterations.labels(agent='coder').observe(iterations)
            except Exception:
                pass  # monitoring optional

            git_diff = extract_git_diff(wt_path)
            final_git_diff = git_diff

            # 🔥 添加诊断日志
            logger.info(f"[Task #{task.task_id}] Git diff extracted: {len(git_diff)} characters")
            if git_diff:
                logger.debug(f"[Task #{task.task_id}] Diff preview: {git_diff[:200]}...")
            else:
                logger.warning(f"[Task #{task.task_id}] No git diff detected! Possible reasons: Planner failed, Coder did not modify files, or all changes were committed before extraction.")

            if not git_diff or "(no changes detected)" in git_diff.lower():
                logger.info(f"[Task #{task.task_id}] Breaking revision loop due to no changes")
                break

            review_result = review_code(task.description, git_diff, previous_critiques=critique_history)
            decision = review_result.get("decision", "APPROVE")
            critique = review_result.get("critique", "")

            if decision == "APPROVE":
                is_approved = True
                break
            else:
                revision_count += 1
                critique_history.append(critique)

                if revision_count >= max_revisions:
                    break

                if card_msg_id:
                    card_session.update(
                        update_func=update_interactive_card,
                        status="running",
                        details=f"⚠️ 第 {revision_count} 轮 Review 被打回。\n\n**打回原因:**\n{critique}\n\n🔄 正在触发 Git 回滚，恢复初始状态让 Coder 重新尝试..."
                    )

                mcp.bash_execute(task.task_id, "git reset --hard HEAD")
                mcp.bash_execute(task.task_id, "git clean -fd")

                current_prompt = f"""{enriched_prompt}

[🚨 URGENT FEEDBACK FROM CODE REVIEWER]:
Your PREVIOUS attempt was REJECTED for the following reasons:
{critique}

I have used `git reset --hard` to rollback the codebase to the clean state before you started.
All your previous bad code has been erased.
Please read the `task_plan.md` again and try a DIFFERENT approach to fix the issues mentioned by the reviewer."""

        text_lower = task.description.lower()
        needs_pr = any(kw in text_lower for kw in ["pr", "pull request", "push", "推送"])

        pr_url = None
        if final_git_diff and "(no changes detected)" not in final_git_diff.lower():
            if needs_pr:
                if card_msg_id:
                    card_session.update(
                        update_func=update_interactive_card,
                        status="running",
                        details="🔄 代码修改完毕，正在向 GitHub 推送..."
                    )
                pr_url = push_branch_and_pr(task, wt_path, final_git_diff, is_dynamic=is_dynamic)

                if pr_url:
                    if card_msg_id:
                        card_session.update(
                            update_func=update_interactive_card,
                            status="success",
                            details="✅ 已推送到远端仓库！",
                            pr_url=pr_url
                        )
                else:
                    cleanup_needed = False
                    if card_msg_id:
                        card_session.update(
                            update_func=update_interactive_card,
                            status="failed",
                            details="❌ 推送失败，回复【重试】继续。"
                        )
            else:
                cleanup_needed = False
                with SESSION_CACHE_LOCK:  # 🔒 线程安全写入
                    SESSION_CACHE[task.chat_id] = {
                        "task": task,
                        "wt_path": wt_path,
                        "git_diff": final_git_diff,
                        "is_dynamic": is_dynamic,
                        "is_retry": False
                    }

                md_ticks = "`" * 3
                diff_preview = final_git_diff[:2500] + ("..." if len(final_git_diff) > 2500 else "")

                msg_content = f"✅ 代码修改完毕并保留了现场。\n*(如需合入远端，请直接回复「**提PR**」)*\n\n**修改预览:**\n{md_ticks}diff\n{diff_preview}\n{md_ticks}"

                if card_msg_id:
                    card_session.update(
                        update_func=update_interactive_card,
                        status="success",
                        details=msg_content
                    )
        else:
            if card_msg_id:
                # ⚠️ 修复：无代码变更不应标记为 success，应使用 no_changes 状态
                card_session.update(
                    update_func=update_interactive_card,
                    status="no_changes",
                    details="⚠️ 未检测到代码变更。\n\n可能原因：\n- Planner 未能生成有效的实施计划\n- Coder 认为当前代码已满足需求\n- 任务描述无需代码修改\n\n💡 您可以：\n- 重新提交并更清晰地描述需求\n- 检查 Planner 生成的 plan 文件（如有）"
                )
        # Mark task as successful if we reached here without exception
        task_success = True

    except Exception as e:
        logger.error(f"Task #{task.task_id} failed: {e}")
        if card_msg_id:
            card_session.update(
                update_func=update_interactive_card,
                status="failed",
                details=f"❌ 执行异常崩溃:\n{str(e)}"
            )
        send_text_message(task.chat_id, f"❌ 执行异常崩溃: {str(e)}")

    finally:
        # Record task completion metrics
        duration = time.time() - start_time
        task_duration_seconds.observe(duration)
        status_label = 'success' if task_success else 'failure'
        tasks_completed_total.labels(status=status_label).inc()
        active_tasks.dec()

        # Destroy sandbox container (ignore errors)
        try:
            mcp.sandbox_destroy_container(task.task_id)
            try:
                from utils.monitoring import container_destroy_total
                container_destroy_total.inc()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Failed to destroy container: {e}")

        # 💥 强力扫尾：只有在成功，或者没改代码的情况下，才销毁沙盒目录
        if cleanup_needed:
            if is_dynamic and dynamic_clone_dir and dynamic_clone_dir.exists():
                try:
                    from sandbox.worktree import robust_rmtree
                    robust_rmtree(str(dynamic_clone_dir), max_retries=3, logger=logger)
                except: pass
            elif not is_dynamic and wt_name and target_repo_path:
                # Remove worktree via MCP client
                try:
                    mcp.worktree_remove(task.task_id, name=wt_name, keep_files=False, workdir=target_repo_path)
                    try:
                        from utils.monitoring import worktree_remove_total
                        worktree_remove_total.inc()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"Failed to remove worktree {wt_name}: {e}")


def send_final_report(task: SWETask, git_diff: str, pr_url: str = None):
    if pr_url:
        message = f"✅ **全自动研发完成！**\n\n代码已成功提交，并已向 GitHub 自动创建了 Pull Request。\n🔗 请点击下方链接审查并合并代码:\n{pr_url}"
        send_text_message(task.chat_id, message)
        return

    if git_diff and "(no changes detected)" not in git_diff.lower():
        message = "✅ 研发完成，但由于网络原因向 GitHub 提交 PR 失败。\n💡 你可以随时对我说：【重新提交】。我将尝试重连网络并重新投递此代码。\n这是本次生成的补丁："
        send_text_message(task.chat_id, message)
        
        md_ticks = "`" * 3
        send_text_message(task.chat_id, f"{md_ticks}diff\n{git_diff[:3000]}\n{md_ticks}")
    else:
        message = "⚠️ 未检测到代码变更。"
        send_text_message(task.chat_id, message)


def push_branch_and_pr(task: SWETask, wt_path: Path, git_diff: str, is_retry: bool = False, retry_branch: str = None, is_dynamic: bool = False) -> Optional[str]:
    github_token = config.github_token

    # 动态注入：如果有独立接管的 repo，就覆盖默认配置
    github_repo = task.target_repo or config.github_repo

    if not github_token or not github_repo:
        logger.warning("Missing GITHUB_TOKEN or GITHUB_REPO. Skipping PR.")
        return None

    if is_retry and retry_branch:
        branch_name = retry_branch
        repo_cwd = str(wt_path)
    else:
        unique_id = uuid.uuid4().hex[:4]
        branch_name = f"repoforge/fix-task-{task.task_id}-{unique_id}"
        repo_cwd = str(wt_path)

    try:
        logger.info(f"Pushing branch '{branch_name}' from {repo_cwd} to {github_repo}")

        if not is_retry:
            subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_cwd, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=repo_cwd, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"RepoForge: Automated fix for Feishu task #{task.task_id}"], cwd=repo_cwd, check=True, capture_output=True)
        
        # 强力重推防御
        max_retries = 3
        for attempt in range(max_retries):
            try:
                subprocess.run(["git", "push", "-u", "origin", branch_name, "--force"], cwd=repo_cwd, check=True, capture_output=True)
                break
            except subprocess.CalledProcessError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Git push failed (Network error), attempt {attempt+1}/{max_retries}. Retrying in 5 seconds...")
                    time.sleep(5)
                else:
                    raise e

        logger.info(f"Branch pushed successfully")

        pr_title = f"🤖 RepoForge: Fix for Task #{task.task_id}"
        md_ticks = "`" * 3
        pr_body = (
            f"## 用户请求\n{task.description}\n\n"
            f"## 代码变更\n{md_ticks}diff\n{git_diff[:3000]}\n{md_ticks}\n\n"
            f"---\n*This PR was automatically generated by RepoForge*"
        )

        pr_url = create_github_pr(branch_name, pr_title, pr_body, github_repo, github_token)

        if pr_url:
            with SESSION_CACHE_LOCK:  # 🔒 线程安全删除
                SESSION_CACHE.pop(task.chat_id, None)
            return pr_url
        else:
            raise Exception("Failed to create PR via API")

    except Exception as e:
        logger.error(f"Error in push_branch_and_pr: {e}")
        with SESSION_CACHE_LOCK:  # 🔒 线程安全写入（失败重试场景）
            SESSION_CACHE[task.chat_id] = {
                "task": task,
                "branch_name": branch_name,
                "git_diff": git_diff,
                "is_dynamic": is_dynamic,
                "wt_path": wt_path,
                "is_retry": True
            }
        return None


# 🌟 GitHub API 调用使用重试装饰器
@retry_network(max_attempts=3, base_delay=2.0)
def create_github_pr(branch_name: str, title: str, body: str, repo: str, token: str) -> Optional[str]:
    """Create a GitHub Pull Request with automatic retry on network errors."""
    url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }

    last_error = None

    for base_branch in ["master", "main"]:
        data = {"title": title, "body": body, "head": branch_name, "base": base_branch}

        try:
            # 💥 核心修复：添加 verify=False 忽略本地代理环境产生的 SSL 证书报错
            resp = httpx.post(url, headers=headers, json=data, timeout=30, verify=False)
            if resp.status_code == 201:
                pr_url = resp.json().get("html_url")
                logger.info(f"PR created successfully targeting base '{base_branch}': {pr_url}")
                return pr_url
            elif resp.status_code == 422:
                if "A pull request already exists" in resp.text:
                    list_url = f"https://api.github.com/repos/{repo}/pulls?head={repo.split('/')[0]}:{branch_name}"
                    list_resp = httpx.get(list_url, headers=headers, timeout=10, verify=False)
                    if list_resp.status_code == 200 and len(list_resp.json()) > 0:
                        return list_resp.json()[0].get("html_url")
                elif "invalid" in resp.text.lower():
                    continue
            logger.error(f"PR creation failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            last_error = e
            logger.error(f"Error creating PR: {e}")
            # 继续尝试下一个 base_branch 或让外层重试逻辑处理

    if last_error:
        raise last_error

    return None


def main():
    if not FEISHU_APP_ID:
        logger.error("FEISHU_APP_ID missing in .env")
        return

    # 🔥 修复 Windows 控制台编码问题（emoji 打印）
    if sys.platform == "win32":
        try:
            # 尝试设置 stdout 为 UTF-8 编码
            sys.stdout.reconfigure(encoding='utf-8')
        except AttributeError:
            # Python < 3.7 或无法 reconfigure，忽略
            pass

    # Start Prometheus metrics server
    setup_monitoring(port=8000, disable=False)

    # Set up signal handlers for graceful shutdown
    import signal

    def handle_shutdown(signum, frame):
        global shutting_down
        if shutting_down:
            return
        shutting_down = True
        logger.warning("Received shutdown signal, stopping bot...")
        # Import active_tasks metric
        from utils.monitoring import active_tasks

        # Stop the WebSocket client to prevent new messages
        try:
            cli.stop()
        except Exception as e:
            logger.debug(f"Error stopping client: {e}")

        # Wait for active tasks to complete (up to 30 seconds)
        timeout = 30
        start_wait = time.time()
        while active_tasks._value.get() > 0 and time.time() - start_wait < timeout:
            logger.info(f"Waiting for {active_tasks._value.get()} active tasks to finish...")
            time.sleep(1)

        # 🔥 新增：优雅关闭线程池
        logger.info("Shutting down task executor...")
        TASK_EXECUTOR.shutdown(wait=True, cancel_futures=False)
        logger.info("Task executor shutdown complete")

        # Force cleanup of any remaining sandboxes
        try:
            from mcp_tools.tools import cleanup_all_sandboxes
            count = cleanup_all_sandboxes(WORKDIR, WORKTREES_BASE)
            logger.info(f"Cleaned up {count} sandboxes during shutdown")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(do_p2_im_message_receive_v1).build()
    cli = WsClient(app_id=FEISHU_APP_ID, app_secret=FEISHU_APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO)

    print("🚀 飞书机器人启动成功！")
    cli.start()

if __name__ == "__main__":
    main()