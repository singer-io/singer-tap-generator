# Instructions: Running a Singer Tap for Any API

- Read this whole file
- Always use absolute paths in the terminal
- Do not ask to proceed, just do what you think is best.

## 1. Create a Virtual Environment for Your Generated Tap

- After generating your Singer tap, create a separate Python virtual environment for your tap:
  ```bash
  python3 -m venv /usr/local/share/virtualenvs/<your-tap-name>
  source /usr/local/share/virtualenvs/<your-tap-name>/bin/activate
  ```
  or
  ```bash
  python3 -m venv ~/.virtualenvs/<your-tap-name>
  source ~/.virtualenvs/<your-tap-name>/bin/activate
  ```

- Install any tap-specific dependencies as needed into that virtual environment.
```bash
uv pip install -e /path/to/tap
```

## 2. Update schemas
- Update the stream schemas to valid JSON schema with valid properties based on the API's docs

## 3. Run Discovery

- Create a `tap_config.json` file if it does not exist
- Include `required_config_keys` from the generation config.
  Example `tap_config.json`:
  ```json
  {
    "user_agent": "Stitch Tap (+support@stitchdata.com)",
    "image_version": "3.latest",
    "start_date": "2024-04-10T00:00:00Z",
    "access_token": “redacted”
  }
  ```

- Get the tap to successfully output a catalog
  ```bash
  /path/to/virtualenv/bin/tap-<your-tap-name> --config /path/to/config --discover
  ```

## 4. Troubleshooting

- If you get a `PermissionError`, make sure the tap path points to the tap's executable Python file, not the directory.
- If you see `Permission denied`, check the tap path and file permissions.
- If you see `SyntaxError` in test files, check for incomplete lines (e.g., `creds =`) and fix them (for APIs without authentication, use `creds = []`).
