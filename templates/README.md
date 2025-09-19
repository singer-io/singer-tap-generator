{{ "# tap-" ~ config.tap_name|lower }}

This is a [Singer](https://singer.io) tap that produces JSON-formatted data
following the [Singer
spec](https://github.com/singer-io/getting-started/blob/master/docs/SPEC.md).

This tap:

- Pulls raw data from the [{{config.tap_name}} API].
- Extracts the following resources:
    {% for stream in config.streams %}
    - [{{stream.name|camel_case}}]({{ stream.doc_link if stream.doc_link else 'https://github.com/singer-io'}})

    {% endfor %}
- Outputs the schema for each resource
- Incrementally pulls data based on the input state


{{ "## Streams" | safe }}


{% for stream in config.streams %}
**[{{stream.name}}]({{ stream.doc_link if stream.doc_link else 'https://github.com/singer-io'}})**
{% if stream.url_endpoint %}
- Endpoint: {{ stream.url_endpoint }}
{% endif %}
{% if stream.data_key %}
- Data Key = {{ stream.data_key }}
{% endif %}
- Primary keys: {{ stream.key_properties }}
- Replication strategy: {{ stream.replication_method }}

{% endfor %}


{{ "## Authentication" | safe }}

{{ "## Quick Start" | safe }}

1. Install

    Clone this repository, and then install using setup.py. We recommend using a virtualenv:

    ```bash
    > virtualenv -p python3 venv
    > source venv/bin/activate
    > python setup.py install
    OR
    > cd .../tap-{{config.tap_name|lower}}
    > pip install -e .
    ```
2. Dependent libraries. The following dependent libraries were installed.
    ```bash
    > pip install singer-python
    > pip install target-stitch
    > pip install target-json

    ```
    - [singer-tools](https://github.com/singer-io/singer-tools)
    - [target-stitch](https://github.com/singer-io/target-stitch)

3. Create your tap's `config.json` file.  The tap config file for this tap should include these entries:
   - `start_date` - the default value to use if no bookmark exists for an endpoint (rfc3339 date string)
   - `user_agent` (string, optional): Process and email for API logging purposes. Example: `tap-{{config.tap_name|lower}} <api_user_email@your_company.com>`
   - `request_timeout` (integer, `300`): Max time for which request should wait to get a response. Default request_timeout is 300 seconds.

    ```json
    {
        "start_date": "2019-01-01T00:00:00Z",
        "user_agent": "tap-{{config.tap_name|lower}} <api_user_email@your_company.com>",
        "request_timeout": 300
    }

    ```
    Optionally, also create a `state.json` file. `currently_syncing` is an optional attribute used for identifying the last object to be synced in case the job is interrupted mid-stream. The next run would begin where the last job left off.

    ```json
    {
        "currently_syncing": "dummy_stream1",
        "bookmarks": {
            "dummy_stream1": "2019-09-27T22:34:39.000000Z",
            "dummy_stream2": "2019-09-28T15:30:26.000000Z",
            "dummy_stream3": "2019-09-28T18:23:53Z"
        }
    }
    ```

4. Run the Tap in Discovery Mode
    This creates a catalog.json for selecting objects/fields to integrate:
    ```bash
    tap-{{config.tap_name|lower}} --config config.json --discover > catalog.json
    ```
   See the Singer docs on discovery mode
   {% set discovery_url = "https://github.com/singer-io/getting-started/blob/master/docs/DISCOVERY_MODE.md#discovery-mode" %}
   [here]({{ discovery_url }}).

{% set state_url = "https://github.com/singer-io/getting-started/blob/master/docs/RUNNING_AND_DEVELOPING.md#running-a-singer-tap-with-a-singer-target" %}
5. Run the Tap in Sync Mode (with catalog) and [write out to state file]({{ state_url }})

    For Sync mode:
    ```bash
    > tap-{{config.tap_name|lower}} --config tap_config.json --catalog catalog.json > state.json
    > tail -1 state.json > state.json.tmp && mv state.json.tmp state.json
    ```
    To load to json files to verify outputs:
    ```bash
    > tap-{{config.tap_name|lower}} --config tap_config.json --catalog catalog.json | target-json > state.json
    > tail -1 state.json > state.json.tmp && mv state.json.tmp state.json
    ```
    To pseudo-load to [Stitch Import API](https://github.com/singer-io/target-stitch) with dry run:
    ```bash
    > tap-{{config.tap_name|lower}} --config tap_config.json --catalog catalog.json | target-stitch --config target_config.json --dry-run > state.json
    > tail -1 state.json > state.json.tmp && mv state.json.tmp state.json
    ```

6. Test the Tap
    While developing the {{config.tap_name|lower}} tap, the following utilities were run in accordance with Singer.io best practices:
    {% set code_quality_url = "https://github.com/singer-io/getting-started/blob/master/docs/BEST_PRACTICES.md#code-quality" %}
    Pylint to improve [code quality]({{ code_quality_url }}):
    ```bash
    > pylint tap_{{config.tap_name|lower}} -d missing-docstring -d logging-format-interpolation -d too-many-locals -d too-many-arguments
    ```
    Pylint test resulted in the following score:
    ```bash
    Your code has been rated at 9.67/10
    ```

    {% set singer_check_url = "https://github.com/singer-io/singer-tools#singer-check-tap"  %}
    To [check the tap]({{ singer_check_url }}) and verify working:
    ```bash
    > tap_{{config.tap_name|lower}} --config tap_config.json --catalog catalog.json | singer-check-tap > state.json
    > tail -1 state.json > state.json.tmp && mv state.json.tmp state.json
    ```

    {{ "#### Unit Tests" | safe }}

    Unit tests may be run with the following.

    ```
    python -m pytest --verbose
    ```

    Note, you may need to install test dependencies.

    ```
    pip install -e .'[dev]'
    ```
---

Copyright &copy; 2019 Stitch
