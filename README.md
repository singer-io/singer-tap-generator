
### Hackathon instructions TODO: remove later or update readme 
- [install uv](https://docs.astral.sh/uv/getting-started/installation/)
- Start up VSCode with Copilot enabled in agent mode
- Prompt Copilot with something like: "use the INSTRUCTIONS.md to generate a new tap using this api: https://weatherstack.com/documentation"
- Keep the generated config
- continue the INSTRUCTIONS.md script and make improvements 

# Singer Tap Generator

A powerful command-line tool for generating [Singer](https://www.singer.io/) tap connectors. This tool automates the creation of Singer tap boilerplate code, helping developers quickly start building singer connector.



## Installation

### Set up

```bash
# Clone the repository
git clone https://github.com/yourusername/singer-generator.git
cd singer-generator

# Install jinja2 pypi
pip install jinja2

# Setup pre-commit
pip install pre-commitpip install pre-commit
pre-commit
pre-commit install
```

## Quick Start

1. Create a configuration file `template_config.json`:

```json
{
  "tap_name": "Harvest",
  "required_config_keys": ["refresh_token", "client_id", "client_secret", "start_date", "user_agent"],
  "page_size": 100,
  "next_page_key": "next_page",
  "pagination_key": "page",
  "headers": {"Accept": "application/json"},
  "params": {},
  "auth_header_key": "Authorization",
  "auth_config_key": "access_token",
  "base_url": "https://api.harvestapp.com/v2/",
  "streams": [
    {
      "name": "projects",
      "key_properties": ["id"],
      "replication_method": "FULL_TABLE",
      "path": "projects",
      "data_key": "projects",
      "doc_link": "https://help.getharvest.com/api-v2/projects-api/projects/projects/"
    },
    {
      "name": "clients",
      "key_properties": ["id"],
      "replication_method": "INCREMENTAL",
      "replication_keys": ["updated_at"],
      "path": "clients",
      "data_key": "clients",
      "url_endpoint": "https://api.harvestapp.com/v2/clients",
      "doc_link": "https://help.getharvest.com/api-v2/clients-api/clients/clients/"
    },
    {
      "name": "invoice_payments",
      "key_properties": ["id"],
      "replication_method": "INCREMENTAL",
      "replication_keys": ["updated_at"],
      "path": "invoices/{}/payments",
      "data_key": "invoice_payments",
      "parent": "invoices",
      "doc_link": "https://help.getharvest.com/api-v2/invoices-api/invoices/invoice-payments/"
    },
    {
      "name": "invoices",
      "key_properties": ["id"],
      "replication_method": "INCREMENTAL",
      "replication_keys": ["updated_at"],
      "path": "invoices",
      "data_key": "invoices",
      "children": ["invoice_payments", "invoice_messages", "invoice_line_items"],
      "doc_link": "https://help.getharvest.com/api-v2/invoices-api/invoices/invoices/"
    }
    ...
  ],
  "tap_tester_creds": {
    "client_id": "TAP_HARVEST_CLIENT_ID",
    "client_secret": "TAP_HARVEST_CLIENT_SECRET",
    "refresh_token": "TAP_HARVEST_REFRESH_TOKEN"
    ...
  },
  "author": "Stitch",
  "third_party_dependencies": [
      "singer-python==5.12.1",
      "requests==2.31.0"
  ]
  ...
}
```

2. Generate the tap:

```bash
python3 singer_generator.py -c template_config.json -o output_dir
```

## Configuration File

The configuration file (`template_config.json`) defines the structure of your Singer tap.
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
| url_endpoint | string | API endpoint url | Yes |
| path | string  | Endpoint path the needs to be added with base_url | No |
| data_key | string  | Key for records in API response | No |
| parent | string  | Parent of current stream | No |
| children | array  | Children of current stream | No |
| doc_link | string  | Documentation link for the current stream endpoint | No |