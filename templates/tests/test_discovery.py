"""Test tap discovery mode and metadata."""
from base import {{config.tap_name}}BaseTest
from tap_tester.base_suite_tests.discovery_test import DiscoveryTest


class {{config.tap_name}}DiscoveryTest(DiscoveryTest, {{config.tap_name}}BaseTest):
    """Test tap discovery mode and metadata conforms to standards."""

    @staticmethod
    def name():
        return "tap_tester_{{ config.tap_name|lower }}_discovery_test"

    def streams_to_test(self):
        return self.expected_stream_names()

