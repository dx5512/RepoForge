"""
SandboxController - Docker Container Lifecycle Management

CRITICAL: Uses user=uid:gid to ensure file permission consistency (Root Permission Trap fix)
"""

import os
import base64
import docker
import platform
import re
from pathlib import Path
from typing import Optional


def convert_windows_path_for_docker(path: Path) -> str:
    """
    Convert Windows paths to Docker Desktop for Windows compatible format.

    Docker Desktop on Windows uses WSL2 and expects Linux-style paths.
    For example: D:\path\to\dir -> /d/path/to/dir

    Args:
        path: Windows path object

    Returns:
        Docker-compatible path string (always uses forward slashes)
    """
    if platform.system() != "Windows":
        return str(path.resolve())

    # Get the absolute path as a raw string to avoid escape sequence issues
    # Use as_posix() which returns forward slashes
    path_obj = path.resolve()
    path_str = path_obj.as_posix()

    # Match drive letter (e.g., D:) at the beginning and convert to /d
    # as_posix() already gives us forward slashes, so we just need to convert "D:/" to "/d/"
    match = re.match(r'^([a-zA-Z]):/(.*)', path_str)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2)
        return f"/{drive}/{rest}"

    # If no drive letter (network path or already converted), return as is
    return path_str


def get_user_id() -> str:
    """Get UID:GID for container user mapping. Cross-platform."""
    if platform.system() == "Windows":
        return None
    return f"{os.getuid()}:{os.getgid()}"


class SandboxController:
    def __init__(self, image: str = "python:3.11-slim"):
        self.image = image
        self._client = None
        self.container: Optional[docker.models.containers.Container] = None
        self.worktree_path: Optional[Path] = None
        self.task_id: Optional[int] = None

    @property
    def client(self):
        """Lazy initialization of Docker client."""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _setup_container_env(self) -> None:
        """
        Configure Git and install necessary system tools inside the container.
        """
        setup_commands = [
            # 🌟 V2.0 修复：为 slim 镜像动态安装 patch 工具，支撑 apply_patch 技能
            'apt-get update && apt-get install -y patch',
            
            # 基础 Git 配置
            'git config --global user.email "agent@repoforge.local"',
            'git config --global user.name "SuperAgent"',
            'git config --global --add safe.directory \'*\'',
        ]

        for cmd in setup_commands:
            # 加上 user="root" 确保 apt-get 有权限执行
            self.container.exec_run(f"bash -c '{cmd}'", user="root")

    def create_container(self, task_id: int, worktree_path: Path) -> str:
        """
        Create and start a Docker container with proper user permissions.
        """
        self.task_id = task_id
        self.worktree_path = worktree_path

        # 💥 核心修复 1：将工作区挂载点统一锁定为 /workspace，与 SuperAgent 完美对齐
        container_path = "/workspace"

        # 💥 Windows 路径转换：将 Windows 路径转换为 Docker Desktop 兼容的 WSL2 路径
        host_path = convert_windows_path_for_docker(worktree_path)

        user_arg = get_user_id()

        try:
            kwargs = {
                "image": self.image,
                "command": "tail -f /dev/null",
                "detach": True,
                "volumes": {
                    host_path: {
                        "bind": container_path,
                        "mode": "rw"
                    }
                },
                "working_dir": container_path,
                "stderr": True,
                "stdout": True,
            }
            if user_arg:
                kwargs["user"] = user_arg

            # Debug logging for Windows path conversion
            try:
                import structlog
                logger = structlog.get_logger(__name__)
                logger.info("Creating Docker container",
                           task_id=task_id,
                           original_path=str(worktree_path),
                           docker_host_path=host_path,
                           container_path=container_path)
            except Exception:
                pass

            self.container = self.client.containers.run(**kwargs)

            self._setup_container_env()

            return f"Container {self.container.short_id} created for task {task_id}"
        except docker.errors.APIError as e:
            # Provide more helpful error message for Windows path issues
            error_msg = str(e)
            if "system cannot find the file specified" in error_msg.lower() or "createfile" in error_msg.lower():
                helpful_msg = (
                    f"Docker failed to mount volume. This is typically a Windows path issue.\n"
                    f"Original path: {worktree_path}\n"
                    f"Docker path: {host_path}\n"
                    f"Ensure:\n"
                    f"  1. Docker Desktop is running\n"
                    f"  2. The directory exists\n"
                    f"  3. Docker has permission to access the directory"
                )
                return f"Docker API Error: {helpful_msg}\nOriginal: {e}"
            return f"Docker API Error: {e}"

    def execute_in_sandbox(self, command: str, timeout: int = 120) -> dict:
        """
        Execute a command inside the sandbox container.
        """
        if not self.container:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "error": "No container running"
            }

        try:
            encoded_command = base64.b64encode(command.encode('utf-8')).decode()
            
            # 💥 核心修复 2：将临时执行脚本放在容器的 /tmp 目录，彻底杜绝污染 Git 代码库
            script_path = "/tmp/.run.sh"

            write_result = self.container.exec_run(
                f"bash -c 'echo {encoded_command} | base64 -d > {script_path}'",
                demux=False
            )

            if write_result.exit_code != 0:
                return {
                    "stdout": "",
                    "stderr": f"Failed to write script: {write_result.output}",
                    "exit_code": write_result.exit_code,
                    "error": "Script write failed"
                }

            result = self.container.exec_run(
                f"bash {script_path}",
                demux=True,
                # 💥 核心修复 3：统一执行目录为 /workspace
                workdir="/workspace"
            )

            stdout, stderr = result.output
            return {
                "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
                "exit_code": result.exit_code,
                "error": None
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "error": str(e)
            }

    def destroy_container(self) -> str:
        """Stop and remove the container."""
        if self.container:
            try:
                self.container.stop(timeout=5)
                self.container.remove(force=True)
                self.container = None
                return f"Container for task {self.task_id} destroyed"
            except docker.errors.APIError as e:
                return f"Error destroying container: {e}"
        return "No container to destroy"

    def is_running(self) -> bool:
        """Check if container is running."""
        if not self.container:
            return False
        try:
            self.container.reload()
            return self.container.status == "running"
        except docker.errors.NotFound:
            return False

    def get_container_info(self) -> dict:
        """Get container information."""
        if not self.container:
            return {"status": "no container"}
        try:
            self.container.reload()
            return {
                "id": self.container.short_id,
                "status": self.container.status,
                "image": self.container.image.tags[0] if self.container.image.tags else self.image,
                "task_id": self.task_id,
                "worktree": str(self.worktree_path)
            }
        except docker.errors.NotFound:
            return {"status": "container not found"}