"""
SandboxController - Docker Container Lifecycle Management

CRITICAL: Uses user=uid:gid to ensure file permission consistency (Root Permission Trap fix)
"""

import os
import base64
import docker
import platform
from pathlib import Path
from typing import Optional


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

    def _setup_git_config(self) -> None:
        """
        Configure Git inside the container to prevent permission errors.

        PATCH: Fixes 'git diff' issue by setting up git user config
        and marking directories as safe.
        """
        git_setup_commands = [
            'git config --global user.email "agent@auto-swe-deer.local"',
            'git config --global user.name "SuperAgent"',
            'git config --global --add safe.directory \'*\'',
        ]

        for cmd in git_setup_commands:
            self.container.exec_run(f"bash -c '{cmd}'")

    def create_container(self, task_id: int, worktree_path: Path) -> str:
        """
        Create and start a Docker container with proper user permissions.

        CRITICAL FIX (Root Permission Trap):
        - Uses user=f"{os.getuid()}:{os.getgid()}" to ensure container
          runs as the host user, preventing permission issues when accessing
          worktree files from host after container exits.
        - On Windows, user mapping is not needed (WSL handles permissions).

        PATCH: Git config setup to prevent 'git diff' permission errors.
        """
        self.task_id = task_id
        self.worktree_path = worktree_path

        container_path = f"/workspace/{task_id}"

        user_arg = get_user_id()

        try:
            kwargs = {
                "image": self.image,
                "command": "tail -f /dev/null",
                "detach": True,
                "volumes": {
                    str(worktree_path.resolve()): {
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

            self.container = self.client.containers.run(**kwargs)

            self._setup_git_config()

            return f"Container {self.container.short_id} created for task {task_id}"
        except docker.errors.APIError as e:
            return f"Docker API Error: {e}"

    def execute_in_sandbox(self, command: str, timeout: int = 120) -> dict:
        """
        Execute a command inside the sandbox container.

        PATCH: Instead of passing complex quoted strings directly to
        'docker exec bash -c "..."' which causes escaping nightmares,
        we write the command to a temporary shell script (.run.sh)
        inside the container/worktree, then execute 'bash .run.sh'.

        Returns dict with 'stdout', 'stderr', 'exit_code', 'error' keys.
        """
        if not self.container:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "error": "No container running"
            }

        try:
            encoded_command = base64.b64encode(command.encode()).decode()
            script_path = f"/workspace/{self.task_id}/.run.sh"

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
                workdir=f"/workspace/{self.task_id}"
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
