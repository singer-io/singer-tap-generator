from base import {{config.tap_name}}BaseTest
from tap_tester.base_suite_tests.bookmark_test import BookmarkTest


class {{config.tap_name}}BookMarkTest(BookmarkTest, {{config.tap_name}}BaseTest):
    """Test tap sets a bookmark and respects it for the next sync of a
    stream."""
    bookmark_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    initial_bookmarks = {
        "bookmarks": {
            {% for stream in config.streams %}
            {% if stream.get("replication_keys") %}
            "{{ stream.name }}": { "{{stream.replication_keys[0]}}" : "2020-01-01T00:00:00Z"},
            {% endif %}
            {% endfor %}
        }
    }
    @staticmethod
    def name():
        return "tap_tester_{{ config.tap_name|lower }}_bookmark_test"

    def streams_to_test(self):
        streams_to_exclude = {}
        return self.expected_stream_names().difference(streams_to_exclude)
