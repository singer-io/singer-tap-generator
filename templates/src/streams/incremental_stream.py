from typing import Dict, Iterator, List

from singer import Transformer, get_logger, metrics, write_record
from singer.utils import strftime, strptime_to_utc

from {{tap_name}}.streams.abstracts import IncrementalStream

LOGGER = get_logger()

class {{ stream.name|capitalize }}(IncrementalStream, PageSizeMixin):
    tap_stream_id = '{{ stream.name }}'
    key_properties = {{ stream.key_properties }}
    replication_keys = {{ stream.replication_keys }}
    {% if stream.data_key %}
    data_key = '{{ stream.data_key }}'
    {% endif %}
    {% if stream.url_endpoint %}
    url_endpoint = '{{ stream.url_endpoint }}'
    {% endif %}
    {% if stream.pagination %}
    pagination = {{ stream.pagination }}
    {% endif %}
    {% if stream.params %}
    params = {{ stream.params }}
    {% endif %}
