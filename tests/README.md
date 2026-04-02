# Tests for RepoForge

## Running Tests

Install development dependencies:
```bash
pip install -r requirements.txt
```

Run all tests:
```bash
pytest tests/ -v
```

Run specific test file:
```bash
pytest tests/test_interceptor.py -v
pytest tests/test_worktree_manager.py -v
```

## Test Coverage

### test_interceptor.py
- Dangerous command blocking (rm -rf, sudo, shutdown, etc.)
- Safe command allowance (ls, cat, git, etc.)
- Empty command rejection
- Case-insensitive pattern matching
- Path validation (within allowed directory)
- Path traversal attack prevention
- Absolute path rejection

### test_worktree_manager.py
- Worktree creation, listing, retrieval
- Worktree removal
- Duplicate name prevention
- Invalid name validation
- Git status retrieval
- Robust directory removal (robust_rmtree)
- Multiple worktree isolation

## Notes

- `test_worktree_manager.py` requires `git` to be installed and available in PATH
- Tests use temporary directories and automatically clean up
- Some tests may be skipped on Windows if git is not configured
