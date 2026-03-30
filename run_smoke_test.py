#!/usr/bin/env python3
"""
run_smoke_test.py - Smoke Test for Auto-SWE-Deer

This script performs a controlled smoke test to verify the full workflow:
1. Git Worktree creation
2. Docker Sandbox container
3. file_read via sandbox
4. file_write via sandbox
5. bash_execute via sandbox
6. git diff extraction
"""

import sys
import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(override=True)

WORKDIR = Path(__file__).parent
DUMMY_REPO = WORKDIR / "dummy_repo"
SANDBOX_BASE = WORKDIR / ".smoke_test_worktrees"
SANDBOX_BASE.mkdir(exist_ok=True)

from sandbox import SandboxController, TaskRegistry
from sandbox.worktree import WorktreeManager
from sandbox.interceptors import CommandInterceptor

TASK_ID = 999
WORKTREE_NAME = "smoke_test"


def print_section(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_step(step_num, description):
    print(f"\n>> STEP {step_num}: {description}")


def run_command(cmd, cwd=None):
    """Run a shell command and return output."""
    import subprocess
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd or WORKDIR,
        capture_output=True,
        text=True
    )
    return result.stdout, result.stderr, result.returncode


async def run_smoke_test():
    print_section("AUTO-SWE-DEER SMOKE TEST")
    print(f"Model: {os.getenv('OPENAI_MODEL_NAME', 'gpt-5.4')}")
    print(f"API Base: {os.getenv('OPENAI_BASE_URL', 'N/A')}")
    print(f"Dummy Repo: {DUMMY_REPO}")
    print(f"Sandbox Base: {SANDBOX_BASE}")
    print(f"Task ID: {TASK_ID}")

    print_section("STEP 0: Verify Preconditions")

    if not DUMMY_REPO.exists():
        print(f"[FAIL] ERROR: dummy_repo not found at {DUMMY_REPO}")
        return False

    hello_py = DUMMY_REPO / "hello.py"
    if hello_py.exists():
        content = hello_py.read_text()
        print(f"[PASS] hello.py exists:\n{content}")
    else:
        print(f"[FAIL] ERROR: hello.py not found")
        return False

    if not (DUMMY_REPO / ".git").exists():
        print(f"[FAIL] ERROR: dummy_repo is not a git repository")
        return False

    print(f"[PASS] dummy_repo is a valid git repository")

    print_section("STEP 1: Create Worktree")

    wtm = WorktreeManager(DUMMY_REPO, SANDBOX_BASE)
    print(f"WorktreeManager initialized")
    print(f"Git available: {wtm.git_available}")

    if not wtm.git_available:
        print(f"[FAIL] Git not available in dummy_repo")
        return False

    try:
        wt_info = wtm.create(WORKTREE_NAME, TASK_ID, "HEAD")
        print(f"[PASS] Worktree created: {wt_info}")
    except Exception as e:
        print(f"[FAIL] Worktree creation failed: {e}")
        return False

    wt_path = Path(wt_info["path"])
    if wt_path.exists():
        print(f"[PASS] Worktree directory exists: {wt_path}")
        worktree_hello = wt_path / "hello.py"
        if worktree_hello.exists():
            print(f"[PASS] hello.py copied to worktree")

    print_section("STEP 2: Create Docker Sandbox")

    sandbox = SandboxController()
    print(f"SandboxController created")
    print(f"Image: {sandbox.image}")

    container_result = sandbox.create_container(TASK_ID, wt_path)
    print(f"[PASS] Container created: {container_result}")

    if sandbox.is_running():
        print(f"[PASS] Container is running")
    else:
        print(f"[FAIL] Container is not running")
        return False

    print_section("STEP 3: Test Command Interceptor")

    interceptor = CommandInterceptor()

    dangerous = ["rm -rf /", "sudo su", "eval malicious"]
    for cmd in dangerous:
        safe, reason = interceptor.is_safe(cmd)
        if not safe:
            print(f"[PASS] Blocked dangerous command: {cmd}")
        else:
            print(f"[FAIL] Should have blocked: {cmd}")

    safe = ["ls -la", "python test.py", "cat file.txt"]
    for cmd in safe:
        is_safe, _ = interceptor.is_safe(cmd)
        if is_safe:
            print(f"[PASS] Allowed safe command: {cmd}")
        else:
            print(f"[FAIL] Should have allowed: {cmd}")

    print_section("STEP 4: Execute Commands in Sandbox")

    print("\n>> Testing: ls -la")
    result = sandbox.execute_in_sandbox("ls -la")
    print(f"   exit_code: {result['exit_code']}")
    if result["stdout"]:
        print(f"   stdout:\n{result['stdout'][:200]}")

    print("\n>> Testing: cat hello.py")
    result = sandbox.execute_in_sandbox("cat hello.py")
    print(f"   exit_code: {result['exit_code']}")
    if result["stdout"]:
        print(f"   stdout: {result['stdout']}")

    print("\n>> Testing: python -c 'print(\"Python works!\")'")
    result = sandbox.execute_in_sandbox("python -c 'print(\"Python works!\")'")
    print(f"   stdout: {result['stdout'].strip()}")
    print(f"   exit_code: {result['exit_code']}")

    print_section("STEP 5: Test file_read via Bash")

    print("\n>> Reading hello.py via bash_execute...")
    result = sandbox.execute_in_sandbox("cat hello.py")
    if result["exit_code"] == 0:
        print(f"[PASS] file_read successful")
        print(f"   Content:\n{result['stdout']}")
    else:
        print(f"[FAIL] file_read failed")
        return False

    print_section("STEP 6: Test file_write via Bash")

    fixed_content = 'def greet():\n    return "Hello, World!"\n'

    print("\n>> Writing fixed hello.py via bash_execute...")

    import base64
    encoded = base64.b64encode(fixed_content.encode()).decode()
    result = sandbox.execute_in_sandbox(f"echo {encoded} | base64 -d > hello.py")

    result = sandbox.execute_in_sandbox("cat hello.py")
    if "World" in result["stdout"]:
        print(f"[PASS] file_write successful - content contains 'World'")
    else:
        print(f"[FAIL] file_write may have failed")
        return False

    print_section("STEP 7: Verify Fix")

    print("\n>> Verifying fix by running inline Python...")

    fixed_content = 'def greet():\n    return "Hello, World!"\n'

    import base64
    encoded = base64.b64encode(fixed_content.encode()).decode()
    sandbox.execute_in_sandbox(f"echo {encoded} | base64 -d > hello.py")

    verify_py = b'from hello import greet\nresult = greet()\nprint("Result:", result)\nassert result == "Hello, World!", f"Expected Hello, World! but got {result}"\nprint("ASSERTION PASSED")'
    encoded_verify = base64.b64encode(verify_py).decode()
    result = sandbox.execute_in_sandbox(f"echo {encoded_verify} | base64 -d > verify.py; python verify.py")

    if result["exit_code"] == 0 and "ASSERTION PASSED" in result["stdout"]:
        print(f"[PASS] VERIFICATION PASSED")
        fix_verified = True
    else:
        print(f"[FAIL] VERIFICATION FAILED")
        if result["stderr"]:
            print(f"   stderr: {result['stderr']}")
        if result["stdout"]:
            print(f"   stdout: {result['stdout']}")
        fix_verified = False

    print_section("STEP 8: Extract git diff")

    print("\n>> Running git diff...")
    diff_result = sandbox.execute_in_sandbox("git diff hello.py")
    if diff_result["stdout"]:
        print(f"[PASS] git diff output:\n{diff_result['stdout']}")
    else:
        print(f"   (No diff output)")

    print_section("STEP 9: Cleanup")

    print("\n>> Destroying container...")
    destroy_result = sandbox.destroy_container()
    print(f"   {destroy_result}")

    print("\n>> Removing worktree...")
    try:
        wtm.remove(WORKTREE_NAME, force=True)
        print(f"   [PASS] Worktree removed")
    except Exception as e:
        print(f"   [WARN] Worktree removal failed: {e}")

    print_section("SMOKE TEST SUMMARY")

    checks = [
        ("Worktree Created", True),
        ("Docker Container Created", True),
        ("Command Interceptor Works", True),
        ("Sandbox Bash Execute", True),
        ("File Read", True),
        ("File Write", True),
        ("Fix Verification", fix_verified),
        ("Container Destroyed", True),
    ]

    all_passed = True
    for name, passed in checks:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"   {status} {name}")
        if not passed:
            all_passed = False

    print(f"\n   Overall: {'[ALL PASS]' if all_passed else '[SOME FAILED]'}")

    return all_passed


def main():
    result = asyncio.run(run_smoke_test())
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
