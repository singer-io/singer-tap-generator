from typing import Dict, Iterator, List

from singer import Transformer, get_logger, metrics, write_record
from singer.utils import strftime, strptime_to_utc

from {{tap_name}}.streams.abstracts import IncrementalStream

LOGGER = get_logger()

class {{ stream.name|camel_case }}(IncrementalStream):
    tap_stream_id = "{{ stream.name }}"
    key_properties = {{ stream.key_properties }}
    replication_method = "INCREMENTAL"
    {% if stream.replication_keys %}
    replication_keys = "{{ stream.replication_keys }}"
    {% endif %}
    {% if stream.data_key %}
    data_key = "{{ stream.data_key }}"
    {% endif %}
    {% if stream.url_endpoint %}
    url_endpoint = "{{ stream.url_endpoint }}"
    {% endif %}
    {% if stream.params %}
    params = {{ stream.params }}
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
    parent = "{{ stream.parent }}"
    {% endif %}
    {% if stream.children %}
    path = "{{ stream.children }}"
    {% endif %}

    def get_bookmark(self, state: dict, key: Any = None) -> int:
        """A wrapper for singer.get_bookmark to deal with compatibility for
        bookmark values or start values."""

        min_parent_bookmark = super().get_bookmark(state) if self.is_selected() else None
        for child in self.child_to_sync:
            if child.is_selected():
                bookmark_key = f"{self.tap_stream_id}_{self.replication_keys[0]}"
                child_bookmark = get_bookmark(state, child.tap_stream_id, bookmark_key, self.client.config.get("start_date"))
                if min_parent_bookmark:
                    min_parent_bookmark = min(min_parent_bookmark, child_bookmark)
                else:
                    min_parent_bookmark = child_bookmark

        return min_parent_bookmark

    def write_bookmark(self, state: dict, key: Any = None, value: Any = None) -> Dict:
        """A wrapper for singer.get_bookmark to deal with compatibility for
        bookmark values or start values."""
        if self.is_selected():
            self.write_bookmark(state, value=current_max_bookmark_date)

        for child in self.child_to_sync:
            if child.is_selected():
                bookmark_key = f"{self.tap_stream_id}_{self.replication_keys[0]}"
                write_bookmark(
                    state, child.tap_stream_id, bookmark_key, value
                )
