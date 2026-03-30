#!/usr/bin/env python3
# Harness: all mechanisms combined -- the complete cockpit for the model.
"""
s_full.py - Full Reference Agent

Capstone implementation combining every mechanism from s01-s11.
Session s12 (task-aware worktree isolation) is taught separately.
NOT a teaching session -- this is the "put it all together" reference.
综合运用s01至s11所有机制的顶点项目实施。
s12环节（任务感知型工作树隔离）将单独讲授。
本内容并非教学课程——而是“整合所有内容”的参考资料。

    +------------------------------------------------------------------+
    |                        FULL AGENT                                 |
    |                                                                   |
    |  System prompt (s05 skills, task-first + optional todo nag)      |
    |                                                                   |
    |  Before each LLM call:                                            |
    |  +--------------------+  +------------------+  +--------------+  |
    |  | Microcompact (s06) |  | Drain bg (s08)   |  | Check inbox  |  |
    |  | Auto-compact (s06) |  | notifications    |  | (s09)        |  |
    |  +--------------------+  +------------------+  +--------------+  |
    |                                                                   |
    |  Tool dispatch (s02 pattern):                                     |
    |  +--------+----------+----------+---------+-----------+          |
    |  | bash   | read     | write    | edit    | TodoWrite |          |
    |  | task   | load_sk  | compress | bg_run  | bg_check  |          |
    |  | t_crt  | t_get    | t_upd    | t_list  | spawn_tm  |          |
    |  | list_tm| send_msg | rd_inbox | bcast   | shutdown  |          |
    |  | plan   | idle     | claim    |         |           |          |
    |  +--------+----------+----------+---------+-----------+          |
    |                                                                   |
    |  Subagent (s04):  spawn -> work -> return summary                 |
    |  Teammate (s09):  spawn -> work -> idle -> auto-claim (s11)      |
    |  Shutdown (s10):  request_id handshake                            |
    |  Plan gate (s10): submit -> approve/reject                        |
    +------------------------------------------------------------------+

    REPL commands: /compact /tasks /team /inbox
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

WORKDIR = Path.cwd()

# === OpenAI 适配：初始化客户端 ===
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", os.getenv("API_KEY")),
    base_url=os.getenv("OPENAI_BASE_URL", os.getenv("BASE_URL")),
    timeout=60.0
)
MODEL = os.environ.get("MODEL_ID", os.environ.get("MODEL"))

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

# 调整了 Token 阈值，大多数 OpenAI 兼容模型的安全上限
TOKEN_THRESHOLD = 80000 
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}


# === 模块：基础工具 (Base Tools) ===
# 提供最底层的文件系统访问和 Shell 命令执行能力
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text(encoding="utf-8")
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# === 模块：轻量级待办追踪 (Todos) ===
# 用于单 Agent 的短生命周期任务打点确认
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """保证数据合法、数量合规、同时最多一个任务在进行中"""
        # ip: 统计进行中的条目数量, validated: 存放校验通过的项目
        validated, ip = [], 0
        # 遍历itme，提取并清洗字段
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content: raise ValueError(f"Item {i}: content required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status '{status}'")
            if not af: raise ValueError(f"Item {i}: activeForm required")
            if status == "in_progress": ip += 1
            validated.append({"content": content, "status": status, "activeForm": af})
        if len(validated) > 20: raise ValueError("Max 20 todos")
        if ip > 1: raise ValueError("Only one in_progress allowed")
        self.items = validated # 校验通过后把合法数据赋值给 self.items
        return self.render()

    def render(self) -> str:
        """把任务列表转换成一段友好的字符串展示出"""
        if not self.items: return "No todos."
        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        """判断 self.items 里有没有未完成的项目"""
        return any(item.get("status") != "completed" for item in self.items)


# === 模块：同步阻塞子代理 (Subagent) ===
# OpenAI 适配：重写内部调用逻辑，转换 Schema 并处理 tool_calls
def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    sub_tools = [
        {"type": "function", "function": {"name": "bash", "description": "Run command.",
         "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read file.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    ]
    if agent_type != "Explore":
        sub_tools += [
            {"type": "function", "function": {"name": "write_file", "description": "Write file.",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "edit_file", "description": "Edit file.",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
        ]
        
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    
    # 注入 System Prompt
    sub_msgs = [
        {"role": "system", "content": f"You are a strictly isolated {agent_type} subagent. Accomplish the user's prompt."},
        {"role": "user", "content": prompt}
    ]
    
    for _ in range(30):
        resp = client.chat.completions.create(model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=4096, temperature=0)
        assistant_msg = resp.choices[0].message
        sub_msgs.append(assistant_msg.model_dump(exclude_none=True))
        
        if not assistant_msg.tool_calls:
            return assistant_msg.content or "(no summary)"
            
        for b in assistant_msg.tool_calls:
            h = sub_handlers.get(b.function.name, lambda **kw: "Unknown tool")
            try:
                args = json.loads(b.function.arguments) if b.function.arguments else {}
                output = str(h(**args))[:50000]
            except Exception as e:
                output = f"Error: {e}"
            sub_msgs.append({"role": "tool", "tool_call_id": b.id, "name": b.function.name, "content": output})
            
    return "(subagent failed: timeout after 30 iterations)"


# === 模块：技能库加载器 (Skills) ===
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text(encoding="utf-8")
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        if not self.skills: return "(no skills)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s: return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# === 模块：上下文压缩引擎 (Compression) ===
def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str, ensure_ascii=False)) // 4

# OpenAI 适配：直接遍历清理 role == "tool" 的内容，保留最近 3 个
def microcompact(messages: list):
    tool_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
    if len(tool_indices) <= 3:
        return
    for i in tool_indices[:-3]:
        if isinstance(messages[i].get("content"), str) and len(messages[i]["content"]) > 100:
            messages[i]["content"] = "[cleared]"

# OpenAI 适配：压缩后必须保留队首的 System Prompt
def auto_compact(messages: list, system_prompt: str) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
            
    conv_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful summarizer."},
            {"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}
        ],
        max_tokens=2000,
    )
    summary = resp.choices[0].message.content
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
        {"role": "assistant", "content": "Understood. Continuing with summary context."},
    ]


# === 模块：持久化任务系统 (Task Board) ===
class TaskManager:
    def __init__(self):
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists(): raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text(encoding="utf-8"))

    def _save(self, task: dict):
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")

    def create(self, subject: str, description: str = "") -> str:
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": [], "blocks": []}
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2, ensure_ascii=False)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text(encoding="utf-8"))
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        tasks = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks: return "No tasks."
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"


# === 模块：异步后台执行管理器 (Background Threads) ===
class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({"task_id": tid, "status": self.tasks[tid]["status"],
                                "result": self.tasks[tid]["result"][:500]})

    def check(self, tid: str = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(running)')}" if t else f"Unknown: {tid}"
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "No bg tasks."

    def drain(self) -> list:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# === 模块：智能体消息总线 (MessageBus) ===
class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        msg = {"type": msg_type, "from": sender, "content": content,
               "timestamp": time.time()}
        if extra: msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists(): return []
        msgs = [json.loads(l) for l in path.read_text(encoding="utf-8").strip().splitlines() if l]
        path.write_text("", encoding="utf-8")
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# === 模块：停机与审批流跟踪 (Protocols) ===
shutdown_requests = {}
plan_requests = {}


# === 模块：自主子节点集群 (Teammate Swarm) ===
class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads = {}

    def _load(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        return {"team_name": "default", "members": []}

    def _save(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name: return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save()
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = (f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
                      f"Use idle when done with current work. You may auto-claim tasks.")
        
        # OpenAI 适配：严格锚定 System Prompt 在队首
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt}
        ]
        
        # 转换子智能体的专属工具列表
        tools = [
            {"type": "function", "function": {"name": "bash", "description": "Run command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
            {"type": "function", "function": {"name": "read_file", "description": "Read file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "write_file", "description": "Write file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "edit_file", "description": "Edit file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
            {"type": "function", "function": {"name": "send_message", "description": "Send message.", "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}}},
            {"type": "function", "function": {"name": "idle", "description": "Signal no more work.", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "claim_task", "description": "Claim task by ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
        ]
        
        while True:
            # -- WORK PHASE --
            for _ in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                try:
                    response = client.chat.completions.create(
                        model=MODEL, messages=messages, tools=tools, max_tokens=4096)
                except Exception as e:
                    print(f"\033[31m  [{name}] API Error: {e}\033[0m")
                    self._set_status(name, "shutdown")
                    return
                    
                assistant_msg = response.choices[0].message
                messages.append(assistant_msg.model_dump(exclude_none=True))
                
                if not assistant_msg.tool_calls:
                    break
                    
                idle_requested = False
                for block in assistant_msg.tool_calls:
                    if block.function.name == "idle":
                        idle_requested = True
                        output = "Entering idle phase."
                    else:
                        args = json.loads(block.function.arguments) if block.function.arguments else {}
                        if block.function.name == "claim_task":
                            output = self.task_mgr.claim(args["task_id"], name)
                        elif block.function.name == "send_message":
                            output = self.bus.send(name, args["to"], args["content"])
                        else:
                            dispatch = {"bash": lambda **kw: run_bash(kw["command"]),
                                        "read_file": lambda **kw: run_read(kw["path"]),
                                        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])}
                            output = dispatch.get(block.function.name, lambda **kw: "Unknown")(**args)
                            
                    print(f"  \033[36m[{name}]\033[0m {block.function.name}: {str(output)[:120]}")
                    messages.append({"role": "tool", "tool_call_id": block.id, "name": block.function.name, "content": str(output)})
                
                if idle_requested:
                    break
                    
            # -- IDLE PHASE: poll for messages and unclaimed tasks --
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                    resume = True
                    break
                    
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text(encoding="utf-8"))
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                        
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    
                    # OpenAI 适配：重新注入身份时避开 0 号 System 索引
                    if len(messages) <= 3:
                        messages.insert(1, {"role": "user", "content": f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        messages.insert(2, {"role": "assistant", "content": f"I am {name}. Continuing."})
                        
                    messages.append({"role": "user", "content": f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break
                    
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]: return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


# === 模块：全局实例装载 (Global Instances) ===
TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)

# 主控 System Prompt
SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Prefer task_create/task_update/task_list for multi-step work. Use TodoWrite for short checklists.
Use task for subagent delegation. Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""


# === 模块：主节点业务处理器 (Lead Protocol Handlers) ===
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"

def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    req = plan_requests.get(request_id)
    if not req: return f"Error: Unknown plan request_id '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"


# === 模块：工具分发与 Schema (Tools Dispatch) ===
TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    "compress":         lambda **kw: "Compressing...",
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("add_blocks")),
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":             lambda **kw: "Lead does not idle.",
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
}

# OpenAI 适配：将所有 23 个工具 Schema 转换为严格的 JSON Schema type: function 格式
TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read file contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace exact text in file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "TodoWrite", "description": "Update task tracking list.", "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}}},
    {"type": "function", "function": {"name": "task", "description": "Spawn a subagent for isolated exploration or work.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}}},
    {"type": "function", "function": {"name": "load_skill", "description": "Load specialized knowledge by name.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "compress", "description": "Manually compress conversation context.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "background_run", "description": "Run command in background thread.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "check_background", "description": "Check background task status.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "task_create", "description": "Create a persistent file task.", "parameters": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}}},
    {"type": "function", "function": {"name": "task_get", "description": "Get task details by ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "task_update", "description": "Update task status or dependencies.", "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "add_blocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "task_list", "description": "List all tasks.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "spawn_teammate", "description": "Spawn a persistent autonomous teammate.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}}},
    {"type": "function", "function": {"name": "list_teammates", "description": "List all teammates.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "send_message", "description": "Send a message to a teammate.", "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}}},
    {"type": "function", "function": {"name": "read_inbox", "description": "Read and drain the lead's inbox.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "broadcast", "description": "Send message to all teammates.", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {"name": "shutdown_request", "description": "Request a teammate to shut down.", "parameters": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}}},
    {"type": "function", "function": {"name": "plan_approval", "description": "Approve or reject a teammate's plan.", "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}}},
    {"type": "function", "function": {"name": "idle", "description": "Enter idle state.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "claim_task", "description": "Claim a task from the board.", "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
]


# === 模块：核心运转循环 (Agent Loop) ===
def agent_loop(messages: list):
    rounds_without_todo = 0
    while True:
        # 清理旧有的冗长工具结果
        microcompact(messages)
        
        # 触发全局 Token 压缩
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("\033[33m[auto-compact triggered]\033[0m")
            messages[:] = auto_compact(messages, SYSTEM)
            
        # 获取后台任务队列回调，以 user 消息注入
        notifs = BG.drain()
        if notifs:
            txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})
            
        # 获取信箱的团队回执，以 user 消息注入
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>"})
            messages.append({"role": "assistant", "content": "Noted inbox messages."})
            
        # 请求大模型
        response = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, max_tokens=4096, temperature=0
        )
        
        assistant_msg = response.choices[0].message
        messages.append(assistant_msg.model_dump(exclude_none=True))
        
        if not assistant_msg.tool_calls:
            return
            
        used_todo = False
        manual_compress = False
        
        for block in assistant_msg.tool_calls:
            if block.function.name == "compress":
                manual_compress = True
                
            handler = TOOL_HANDLERS.get(block.function.name)
            try:
                args = json.loads(block.function.arguments) if block.function.arguments else {}
                output = handler(**args) if handler else f"Unknown tool: {block.function.name}"
            except Exception as e:
                output = f"Error: {e}"
                
            print(f"> \033[35m{block.function.name}\033[0m: {str(output)[:200]}")
            
            # 标准 Role Tool 记录结果
            messages.append({"role": "tool", "tool_call_id": block.id, "name": block.function.name, "content": str(output)})
            
            if block.function.name == "TodoWrite":
                used_todo = True
                
        # 唠叨提醒（nag reminder）：分离作为 User 消息追加
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if TODO.has_open_items() and rounds_without_todo >= 3:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
            
        # 手动执行大瘦身
        if manual_compress:
            print("\033[33m[manual compact]\033[0m")
            messages[:] = auto_compact(messages, SYSTEM)


# === 模块：交互终端 (REPL) ===
if __name__ == "__main__":
    # 初始化：确保第一条永远是 System Prompt
    history = [{"role": "system", "content": SYSTEM}]
    
    while True:
        try:
            query = input("\033[36ms_full >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
            
        if query.strip().lower() in ("q", "exit", ""):
            break
            
        if query.strip() == "/compact":
            if history:
                print("\033[33m[manual compact via /compact]\033[0m")
                history[:] = auto_compact(history, SYSTEM)
            continue
        if query.strip() == "/tasks":
            print(TASK_MGR.list_all())
            continue
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False))
            continue
            
        history.append({"role": "user", "content": query})
        
        try:
            agent_loop(history)
        except Exception as e:
            print(f"\n\033[31m[API Error] {e}\033[0m\n")
            continue
            
        # 解析最终输出文本
        last_message = history[-1]
        if last_message.get("role") == "assistant" and last_message.get("content"):
            print(last_message["content"])
        print()