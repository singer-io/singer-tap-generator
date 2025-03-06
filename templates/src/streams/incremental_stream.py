from typing import Dict, Any
from singer import get_bookmark, get_logger
from {{tap_name}}.streams.abstracts import IncrementalStream

LOGGER = get_logger()


class {{ stream.name|camel_case }}(IncrementalStream):
    tap_stream_id = "{{ stream.name }}"
    key_properties = {{ stream.key_properties | tojson}}
    replication_method = "INCREMENTAL"
    {% if stream.replication_keys %}
    replication_keys = {{ stream.replication_keys| tojson }}
    {% endif %}
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
    parent = "{{ stream.parent }}"
    bookmark_value = None
    {% endif %}
    {% if stream.children %}
    children = {{ stream.children| tojson }}
    {% endif %}
    {% if stream.children %}

    def get_bookmark(self, state: Dict, stream: str, key: Any = None) -> int:
        """A wrapper for singer.get_bookmark to deal with compatibility for
        bookmark values or start values."""

       min_parent_bookmark = super().get_bookmark(state, stream) if self.is_selected() else None
        for child in self.child_to_sync:
            if child.is_selected():
                bookmark_key = f"{self.tap_stream_id}_{self.replication_keys[0]}"
                child_bookmark = super().get_bookmark(state, child.tap_stream_id, key=bookmark_key)
                if min_parent_bookmark:
                    min_parent_bookmark = min(min_parent_bookmark, child_bookmark)
                else:
                    min_parent_bookmark = child_bookmark

        return min_parent_bookmark

    def write_bookmark(self, state: Dict, stream: str, key: Any = None, value: Any = None) -> Dict:
        """A wrapper for singer.get_bookmark to deal with compatibility for
        bookmark values or start values."""
        if self.is_selected():
            super().write_bookmark(state, stream, value=value)

        for child in self.child_to_sync:
            if child.is_selected():
                bookmark_key = f"{self.tap_stream_id}_{self.replication_keys[0]}"
                super().write_bookmark(state, child.tap_stream_id, key=bookmark_key, value=value)
        
        return state
    {% endif %}
    {% if stream.parent %}

    def get_bookmark(self, state: Dict, key: Any = None) -> int:
        """
        Return initial bookmark value only for the child stream.
        """
        if not self.bookmark_value:        
            self.bookmark_value = super().get_bookmark(state, key)

        return self.bookmark_value
    {% endif %}
