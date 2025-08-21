import copy
import os
import unittest
from datetime import datetime as dt
from datetime import timedelta

import dateutil.parser
import pytz
from tap_tester import connections, menagerie, runner
from tap_tester.logger import LOGGER
from tap_tester.base_suite_tests.base_case import BaseCase


class {{config.tap_name}}BaseTest(BaseCase):
    """Setup expectations for test sub classes.

    Metadata describing streams. A bunch of shared methods that are used
    in tap-tester tests. Shared tap-specific methods (as needed).
    """
    start_date = "2019-01-01T00:00:00Z"

    @staticmethod
    def tap_name():
        """The name of the tap."""
        return "tap-{{ config.tap_name|lower }}"

    @staticmethod
    def get_type():
        """The name of the tap."""
        return "platform.{{ config.tap_name|lower }}"

    @classmethod
    def expected_metadata(cls):
        """The expected streams and metadata about the streams."""
        return {
            {% for stream in config.streams %}
            "{{ stream.name }}": {
                cls.PRIMARY_KEYS: { {{stream.key_properties|join(", ")|tojson}} },
                cls.REPLICATION_METHOD: cls.{{ stream.get("replication_method", "FULL_TABLE") }},
                {% if stream.get("replication_keys") %}
                cls.REPLICATION_KEYS: { {{stream.replication_keys|join(", ")|tojson}} },
                {% else %}
                cls.REPLICATION_KEYS: set(),
                {% endif %}
                cls.OBEYS_START_DATE: {{ stream.get("obeys_start_date", False)|string }},
                {% if stream.get("additional_metadata") %}
                {% for key, value in stream.additional_metadata.items() %}
                cls.{{ key }}: {{ value }},
                {% endfor %}
                {% endif %}
                cls.API_LIMIT: {{config.page_size if config.page_size else 100}}
            }{% if not loop.last %},{% endif %}

            {% endfor %}
        }

    @staticmethod
    def get_credentials():
        """Authentication information for the test account."""
        credentials_dict = {}
        creds = {{config.tap_tester_creds}}

        for cred in creds:
            credentials_dict[cred] = os.getenv(creds[cred])

        return credentials_dict

    def get_properties(self, original: bool = True):
        """Configuration of properties required for the tap."""
        return_value = {
            "start_date": "2022-07-01T00:00:00Z"
        }
        if original:
            return return_value

        return_value["start_date"] = self.start_date
        return return_value

