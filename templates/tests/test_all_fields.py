from base import {{config.tap_name}}BaseTest
from tap_tester.base_suite_tests.all_fields_test import AllFieldsTest

KNOWN_MISSING_FIELDS = {
    # Add missing fields here
}


class {{config.tap_name}}AllFields(AllFieldsTest, {{config.tap_name}}BaseTest):
    """Ensure running the tap with all streams and fields selected results in
    the replication of all fields."""

    @staticmethod
    def name():
        return "tap_tester_{{ config.tap_name|lower }}_all_fields_test"

    def streams_to_test(self):
        streams_to_exclude = {}
        return self.expected_stream_names().difference(streams_to_exclude)