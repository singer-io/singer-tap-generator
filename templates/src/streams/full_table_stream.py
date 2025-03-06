from typing import Dict, Iterator, List
from singer import get_logger
from {{tap_name}}.streams.abstracts import FullTableStream

LOGGER = get_logger()


class {{ stream.name|camel_case }}(FullTableStream):
    tap_stream_id = "{{ stream.name }}"
    key_properties = {{ stream.key_properties| tojson }}
    replication_method = "FULL_TABLE"
    {% if stream.data_key %}
    data_key = "{{ stream.data_key }}"
    {% endif %}
    {% if stream.url_endpoint %}
    url_endpoint = "{{ stream.url_endpoint }}"
    {% endif %}
    {% if stream.params %}
    params = {{ stream.params| tojson }}
    {% endif %}
    {% if stream.page_size %}
    page_size = {{ stream.page_size }}
    {% endif %}
    {% if stream.next_page_key %}
    next_page_key = "{{ stream.next_page_key }}"
    {% endif %}
    {% if stream.path %}
    path = "{{ stream.path }}"
    {% endif %}
    {% if stream.parent %}
    path = "{{ stream.parent }}"
    {% endif %}
    {% if stream.children %}
    children = "{{ stream.children }}"
    {% endif %}
