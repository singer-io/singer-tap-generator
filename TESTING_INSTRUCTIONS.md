# Instructions: Testing a Singer Tap for Any API

- Read this whole file
- Always use absolute paths in the terminal
- Do not ask to proceed, just do what you think is best.

## 1. Create a Virtual Environment for Your Generated Tap and tap-tester

- Create a Python virtual environment for tap-tester:
  ```bash
  python3 -m venv /usr/local/share/virtualenvs/tap-tester
  source /usr/local/share/virtualenvs/tap-tester/bin/activate
  ```
  or
  ```bash
  python3 -m venv ~/.virtualenvs/tap-tester
  source ~/.virtualenvs/tap-tester/bin/activate
  ```
- Install tap-tester and its dependencies in this environment.

## 2. Testing with tap-tester

- Ensure you have the `tap-tester` repo and its dependencies installed.
- Activate the tap-tester environment.
- Run the test suite (ensure the tap executable is specified, not the directory):
  ```bash
  /path/to/tap-tester/bin/run-test --tap=path/to/virtualenv/bin/tap-<your-tap-name> /path/to/tap-<your-tap-name>/tests/test_discovery.py # Move on to other tests once this passes
  ```
  - Get the `tests/test_discovery.py` test passing before moving on to others.
  - If you get a `PermissionError`, make sure the tap path points to the tap's executable Python file, not the directory.

## 3. Troubleshooting

- If you see `SyntaxError` in test files, check for incomplete lines (e.g., `creds =`) and fix them (for APIs without authentication, use `creds = []`).
- If you see `ModuleNotFoundError: No module named 'jinja2'`, install Jinja2 in your environment.
- If you see `Permission denied`, check the tap path and file permissions.
