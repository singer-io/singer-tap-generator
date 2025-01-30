# Singer Tap Generator

A powerful command-line tool for generating [Singer](https://www.singer.io/) tap connectors. This tool automates the creation of Singer tap boilerplate code, helping developers quickly start building singer connectorco.



## Installation

### Set up

```bash
# Clone the repository
git clone https://github.com/yourusername/singer-generator.git
cd singer-generator

# Install jinja2 pypi
pip install jinja2
```

## Quick Start

1. Create a configuration file `config.json`:

```json
{
  "tap_name": "example",
  "api_url": "https://api.example.com",
  "auth_method": "oauth2",
  "streams": [
    {
      "name": "users",
      "path": "/users",
      "replication_method": "INCREMENTAL",
      "replication_key": "updated_at",
      ...
    },
    {
      "name": "posts",
      "path": "/posts",
      "replication_method": "FULL_TABLE",
      ...
    }
  ],
  ...
}
```

2. Generate the tap:

```bash
singer-generator -c config.json -o output_dir
```

## Configuration File

The configuration file (`config.json`) defines the structure of your Singer tap.
detailed breakdown of the available options:

### Root Level Configuration

| Field | Type | Description | Required |
|---------- | ---------- |----------|----------|
| tap_name | string | Name of your tap | Yes |
| required_config_keys | array | Config keys required for the tap to run | Yes |
| third_party_dependencies | array | Dependencies require to run the tap | Yes |

### Stream Level Configuration
| Field | Type | Description | Required |
|---------- | ---------- |----------|----------|
| name | string |  Name of the stream | Yes |
| key_properties | array |  Primary key fields  | Yes |
| replication_keys | array |  Fields used for incremental replication | Yes |
| replication_method | string | FULL_TABLE or INCREMENTAL | Yes |
| endpoint | string | API endpoint url | Yes |
| data_key | string  | Key for records in API response | No |