from tap_tester.base_suite_tests.pagination_test import PaginationTest
from base import {{config.tap_name}}BaseTest

class {{config.tap_name}}PaginationTest(PaginationTest, {{config.tap_name}}BaseTest):
    """
    Ensure tap can replicate multiple pages of data for streams that use pagination.
    """

    @staticmethod
    def name():
        return "tap_tester_{{ config.tap_name|lower }}_pagination_test"

    def streams_to_test(self):
        streams_to_exclude = {}
        return self.expected_stream_names().difference(streams_to_exclude)

