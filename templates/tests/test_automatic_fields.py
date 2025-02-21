"""Test that with no fields selected for a stream automatic fields are still
replicated."""
from base import {{config.tap_name}}BaseTest
from tap_tester.base_suite_tests.automatic_fields_test import MinimumSelectionTest


class {{config.tap_name}}AutomaticFields(MinimumSelectionTest, {{config.tap_name}}BaseTest):
    """Test that with no fields selected for a stream automatic fields are
    still replicated."""

    @staticmethod
    def name():
        return "tap_tester_{{ config.tap_name|lower }}_automatic_fields_test"

    def streams_to_test(self):
        streams_to_exclude = {}
        return self.expected_stream_names().difference(streams_to_exclude)
