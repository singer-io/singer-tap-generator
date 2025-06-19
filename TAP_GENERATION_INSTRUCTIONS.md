# Instructions: Building a Singer Tap for Any API

- Always use absolute paths in the terminal
- Read the whole file

1. Create a config file (e.g., `my_api_config.json`) with the following structure:
  ### Example Structure
    ```json
    {
      "tap_name": "my_api",
      "required_config_keys": ["<config_key_name>"], // All configuration required to access API
      "headers": {"Accept": "application/json"},
      "params": {},
      "auth_header_key": "Authorization",
      "auth_config_key": "access_token",
      "base_url": "api.my_api.com/v1",
      "streams": [
        {
          "name": "<stream_name>",
          "key_properties": ["<primary_key_field>"],
          "replication_method": "INCREMENTAL || FULL_TABLE",
          "replication_key": "<replication_key>", // Required if `replication_method` is incremental
          "path": "<path>",
          "data_key": "<data_key>",
          "doc_link": "<link-to-api-docs>"
        }
        // Add more streams as needed
      ],
      "tap_tester_creds": {
          "<config_key_name>": "<TAP_NAME>_<CONFIG_KEY_NAME>"
          // Add more config keys to specify all credentials in `required_config_keys`
      },
    }
    ``` 

  ### Requirements
    - Config values should use singer best practices
    - See sample_config.json for a complete example
    - Create a `stream` for each endpoint you see in the API documentation.
    - Replace `tap_name`, `name`, `endpoint`, and `key_properties` with values appropriate for your API.
    - `required_config_keys` list is required and must contain the configuration properties to authenticate with your API
    - `streams` list is required and contains objects that define configuration for each object available from the API
    - `key_properties` list is required and contains the primary key fields of a record in the stream.
    - Use the most appropriate `replication_method` for each stream out of `FULL_TABLE` or `INCREMENTAL`. `INCREMENTAL` is appropriate if there is a datetime value in the stream's fields.
    - The `replication_key` field is required if the stream's`replication_method` is `INCREMENTAL`

2. Double check the API docs and add any endpoints you missed to `streams`.

3. CHECK AGAIN. IT IS VERY IMPORTANT YOU DO NOT MISS ANY STREAMS. LIVES ARE ON THE LINE!

4. Run the generator:
    ```bash
    uv run <path-to-singer-tap-generator>/singer_generator.py -c my_api_config.json -o .
    ```
  The tap will be generated in the `tap-<your-tap-name>` directory.
