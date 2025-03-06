
from base import {{config.tap_name}}BaseTest
from tap_tester.base_suite_tests.interrupted_sync_test import InterruptedSyncTest


class {{config.tap_name}}InterruptedSyncTest({{config.tap_name}}BaseTest):
    """Test tap sets a bookmark and respects it for the next sync of a
    stream."""

    @staticmethod
    def name():
        return "tap_tester_{{ config.tap_name|lower }}_interrupted_sync_test"

    def streams_to_test(self):
        return self.expected_stream_names()


    def manipulate_state(self):
        return {
            "currently_syncing": "prospects",
            "bookmarks": {
                {% for stream in config.streams %}
                {% if stream.get("replication_keys") %}
                "{{ stream.name }}": { "{{stream.replication_keys[0]}}" : "2020-01-01T00:00:00Z"},
                {% endif %}
                {% endfor %}
        }
    }