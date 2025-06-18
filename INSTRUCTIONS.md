# Instructions: Building and Testing a Singer Tap for Any API

## 1. Generate the Singer Tap

- Use the provided `singer_generator.py` to scaffold a new tap:
  1. Create a config file (e.g., `my_api_config.json`) with the following structure:
     ```json
     {
       "tap_name": "my_api",
       "streams": [
         {
           "name": "<stream_name>",
           "replication_method": "FULL_TABLE",
           "endpoint": "<https://api.example.com/endpoint>",
           "key_properties": ["<primary_key_field>"]
         }
         // Add more streams as needed
       ],
        "tap_tester_creds": {
            "client_id": "<tap_name>_CLIENT_ID",
            "client_secret": "<tap_name>_CLIENT_SECRET",
            "refresh_token": "<tap_name>_REFRESH_TOKEN"
       },
     }
     ```
     - Replace `tap_name`, `name`, `endpoint`, and `key_properties` with values appropriate for your API.
     - `key_properties` list is required.
     - Use the most appropriate `replication_method` for each stream. Use `replication_key` if the `replication_method` is `INCREMENTAL`
  
  2. Run the generator:
     ```bash
     uv run singer_generator.py -c my_api_config.json -o .
     ```
  3. The tap will be generated in the `tap-<your_tap_name>` directory.

## 2. Create a Virtual Environment for Your Generated Tap and tap-tester

- After generating your Singer tap, create a separate Python virtual environment for your tap (recommended):
  ```bash
  python3 -m venv /usr/local/share/virtualenvs/<your-tap-name>
  source /usr/local/share/virtualenvs/<your-tap-name>/bin/activate
  ```
- Install any tap-specific dependencies as needed.

- (Recommended) Create a separate Python virtual environment for tap-tester:
  ```bash
  python3 -m venv /usr/local/share/virtualenvs/tap-tester
  source /usr/local/share/virtualenvs/tap-tester/bin/activate
  ```
- Install tap-tester and its dependencies in this environment.

## 3. Testing with tap-tester

- Ensure you have the `tap-tester` repo and its dependencies installed.
- Activate the tap-tester environment.
- Run the test suite (ensure the tap executable is specified, not the directory):
  ```bash
  bin/run-test --tap=/path/to/tap-<your_tap_name> /path/to/tap-<your_tap_name>/tests
  ```
  - If you get a `PermissionError`, make sure the tap path points to the tap's executable Python file, not the directory.

## 4. Troubleshooting

- If you see `SyntaxError` in test files, check for incomplete lines (e.g., `creds =`) and fix them (for APIs without authentication, use `creds = []`).
- If you see `ModuleNotFoundError: No module named 'jinja2'`, install Jinja2 in your environment.
- If you see `Permission denied`, check the tap path and file permissions.

---

These steps are based on a workflow for building and testing Singer taps and should help you repeat or automate the process for any API.
