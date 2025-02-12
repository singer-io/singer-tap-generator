import copy
import os
import unittest
from datetime import datetime as dt
from datetime import timedelta

import dateutil.parser
import pytz
from tap_tester import connections, menagerie, runner
from tap_tester.logger import LOGGER

class {{config.tap_name}}BaseTest(unittest.TestCase):
    """Setup expectations for test sub classes.

    Metadata describing streams. A bunch of shared methods that are used
    in tap-tester tests. Shared tap-specific methods (as needed).
    """

    PRIMARY_KEYS = "table-key-properties"
    START_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
    BOOKMARK_COMPARISON_FORMAT = "%Y-%m-%dT%H:%M:%S.000000Z"
    REPLICATION_KEYS = "valid-replication-keys"
    REPLICATION_METHOD = "forced-replication-method"
    INCREMENTAL = "INCREMENTAL"
    FULL_TABLE = "FULL_TABLE"
    OBEYS_START_DATE = "obey-start-date"
    

    @staticmethod
    def tap_name():
        """The name of the tap."""
        return 'tap-{{ config.tap_name|lower }}'
    
    @staticmethod
    def get_type():
        """The name of the tap."""
        return 'platform.{{ config.tap_name|lower }}'

    def expected_metadata(self):
        """The expected streams and metadata about the streams."""
        return {
            {% for stream in config.streams %}
            "{{ stream.name }}": {
                self.PRIMARY_KEYS: {{ stream.get("key_properties", "set()") }},
                self.REPLICATION_METHOD: self.{{ stream.get("replication_method", "FULL_TABLE") }},
                {% if stream.get("replication_keys") %}
                self.REPLICATION_KEYS: { {{stream.replication_keys|join(', ')|tojson}} },
                {% else %}
                self.REPLICATION_KEYS: set(),
                {% endif %}
                self.OBEYS_START_DATE: {{ stream.get("obeys_start_date", False)|string }},
                {% if stream.get("additional_metadata") %}
                {% for key, value in stream.additional_metadata.items() %}
                self.{{ key }}: {{ value }},
                {% endfor %}
                {% endif %}
            }{% if not loop.last %},{% endif %}

            {% endfor %}
        }

    def expected_streams(self):
        """A set of expected stream names."""
        return set(self.expected_metadata().keys())

    def expected_primary_keys(self):
        """Return a dictionary with key of table name and value as a set of
        primary key fields."""
        return {
            table: properties.get(self.PRIMARY_KEYS, set()) for table, properties in self.expected_metadata().items()
        }

    def expected_replication_method(self):
        """Return a dictionary with key of table name and value of replication
        method."""
        return {
            table: properties.get(self.REPLICATION_METHOD, None)
            for table, properties in self.expected_metadata().items()
        }

    def setUp(self):
        """Checking required environment variables."""
        required_env = {
            {% for key in config.tap_tester_creds.values() %}
            '{{key}}',
            {% endfor %}
        }
        missing_envs = [v for v in required_env if not os.getenv(v)]
        if len(missing_envs) != 0:
            raise Exception("Missing test-required environment variables")
    
    def parse_date(self, date_value):
        """Pass in string-formatted-datetime, parse the value, and return it as
        an unformatted datetime object."""
        date_formats = {
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f+00:00",
            "%Y-%m-%dT%H:%M:%S+00:00",
            "%Y-%m-%d",
        }
        for date_format in date_formats:
            try:
                date_stripped = dt.strptime(date_value, date_format)
                return date_stripped
            except ValueError:
                continue

        raise NotImplementedError(f"Tests do not account for dates of this format: {date_value}")

    def expected_replication_keys(self):
        """Return a dictionary with key of table name and value as a set of
        replication key fields."""
        return {
            table: properties.get(self.REPLICATION_KEYS, set())
            for table, properties in self.expected_metadata().items()
        }

    def get_credentials(self):
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

    def expected_automatic_fields(self):
        """Retrieving primary keys and replication keys as an automatic
        fields."""
        auto_fields = {}
        for k, v in self.expected_metadata().items():
            auto_fields[k] = v.get(self.PRIMARY_KEYS, set()) | v.get(self.REPLICATION_KEYS, set())
        return auto_fields

    def run_and_verify_check_mode(self, conn_id):
        """Run the tap in check mode and verify it succeeds.

        This should be ran prior to field selection and initial sync.
        Return the connection id and found catalogs from menagerie.
        """
        # Run in check mode
        check_job_name = runner.run_check_mode(self, conn_id)

        # Verify check exit codes
        exit_status = menagerie.get_exit_status(conn_id, check_job_name)
        menagerie.verify_check_exit_status(self, exit_status, check_job_name)

        found_catalogs = menagerie.get_catalogs(conn_id)
        self.assertGreater(len(found_catalogs), 0, msg=f"unable to locate schemas for connection {conn_id}")

        found_catalog_names = set(map(lambda c: c["stream_name"], found_catalogs))

        self.assertSetEqual(self.expected_streams(), found_catalog_names, msg="discovered schemas do not match")
        LOGGER.info("discovered schemas are OK")

        return found_catalogs

    def run_and_verify_sync(self, conn_id):
        """Run a sync job and make sure it exited properly.

        Return a dictionary with keys of streams synced and values of
        records synced for each stream
        """
        # Run a sync job using orchestrator
        sync_job_name = runner.run_sync_mode(self, conn_id)

        # Verify tap and target exit codes
        exit_status = menagerie.get_exit_status(conn_id, sync_job_name)
        menagerie.verify_sync_exit_status(self, exit_status, sync_job_name)

        # Verify actual rows were synced
        sync_record_count = runner.examine_target_output_file(
            self, conn_id, self.expected_streams(), self.expected_primary_keys()
        )
        self.assertGreater(sum(sync_record_count.values()), 0, msg=f"failed to replicate any data: {sync_record_count}")
        LOGGER.info(f"total replicated row count: {sum(sync_record_count.values())}")
        return sync_record_count

    def perform_and_verify_table_and_field_selection(self, conn_id, test_catalogs, select_all_fields=True):
        """Perform table and field selection based off of the streams to select
        set and field selection parameters.

        Verify this results in the expected streams selected and all or
        no fields selected for those streams.
        """

        # Select all available fields or select no fields from all testable streams
        self.select_all_streams_and_fields(conn_id=conn_id, catalogs=test_catalogs, select_all_fields=select_all_fields)

        catalogs = menagerie.get_catalogs(conn_id)

        # Ensure our selection affects the catalog
        expected_selected = [tc.get("stream_name") for tc in test_catalogs]
        for cat in catalogs:
            catalog_entry = menagerie.get_annotated_schema(conn_id, cat["stream_id"])

            # Verify all testable streams are selected
            selected = catalog_entry.get("annotated-schema").get("selected")
            LOGGER.info("Validating selection on {}: {}".format(cat["stream_name"], selected))
            if cat["stream_name"] not in expected_selected:
                self.assertFalse(selected, msg="Stream selected, but not testable.")
                continue  # Skip remaining assertions if we aren't selecting this stream
            self.assertTrue(selected, msg="Stream not selected.")

            if select_all_fields:
                # Verify all fields within each selected stream are selected
                for field, field_props in catalog_entry.get("annotated-schema").get("properties").items():
                    field_selected = field_props.get("selected")
                    LOGGER.info("\tValidating selection on {}.{}: {}".format(cat["stream_name"], field, field_selected))
                    self.assertTrue(field_selected, msg="Field not selected.")
            else:
                # Verify only automatic fields are selected
                expected_automatic_fields = self.expected_automatic_fields().get(cat["stream_name"])
                selected_fields = self.get_selected_fields_from_metadata(catalog_entry["metadata"])
                self.assertEqual(expected_automatic_fields, selected_fields)

    @staticmethod
    def get_selected_fields_from_metadata(metadata):
        """Returning the fields which are marked as automatic and marked as
        selected:true."""
        selected_fields = set()
        for field in metadata:
            is_field_metadata = len(field["breadcrumb"]) > 1
            inclusion_automatic_or_selected = (
                field["metadata"]["selected"] is True or field["metadata"]["inclusion"] == "automatic"
            )
            if is_field_metadata and inclusion_automatic_or_selected:
                selected_fields.add(field["breadcrumb"][1])
        return selected_fields

    @staticmethod
    def select_all_streams_and_fields(conn_id, catalogs, select_all_fields=True):
        """Select all streams and all fields within streams."""
        for catalog in catalogs:
            schema = menagerie.get_annotated_schema(conn_id, catalog["stream_id"])

            non_selected_properties = []
            if not select_all_fields:
                # Get a list of all properties so that none are selected
                non_selected_properties = schema.get("annotated-schema", {}).get("properties", {}).keys()

            connections.select_catalog_and_fields_via_metadata(conn_id, catalog, schema, [], non_selected_properties)

    def parse_date(self, date_value):
        """
        Pass in string-formatted-datetime, parse the value, and return it as an unformatted datetime object.
        """
        date_formats = {
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f+00:00",
            "%Y-%m-%dT%H:%M:%S+00:00",
            "%Y-%m-%d",
        }
        for date_format in date_formats:
            try:
                date_stripped = dt.strptime(date_value, date_format)
                return date_stripped
            except ValueError:
                continue

        raise NotImplementedError(
            f"Tests do not account for dates of this format: {date_value}"
        )


    def timedelta_formatted(self, dtime, dt_format, days=0):
        """Checking the datetime format is as per the expectation Adding the
        lookback window days in the date given as an argument."""
        try:
            date_stripped = dt.strptime(dtime, dt_format)
            return_date = date_stripped + timedelta(days=days)
            return dt.strftime(return_date, dt_format)

        except ValueError:
            return Exception(f"Datetime object is not of the format: {dt_format}")

    def calculated_states_by_stream(self, current_state):
        """Look at the bookmarks from a previous sync and set a new bookmark
        value based off timedelta expectations.

        This ensures the subsequent sync will replicate at least 1
        record but, fewer records than the previous sync. If the test
        data is changed in the future this will break expectations for
        this test.
        """
        timedelta_by_stream = {
            stream: [0, 0, 0, 5]  # {stream_name: [days, hours, minutes, seconds], ...}
            for stream in self.expected_streams()
        }

        stream_to_calculated_state = {stream: "" for stream in current_state["bookmarks"].keys()}
        for stream, state in current_state["bookmarks"].items():
            state_format = "%Y-%m-%dT%H:%M:%S-00:00"
            state_key, state_value = next(iter(state.keys())), next(iter(state.values()))
            state_as_datetime = dateutil.parser.parse(state_value)

            days, hours, minutes, seconds = timedelta_by_stream[stream]
            calculated_state_as_datetime = state_as_datetime - timedelta(
                days=days, hours=hours, minutes=minutes, seconds=seconds
            )
            calculated_state_formatted = dt.strftime(calculated_state_as_datetime, state_format)

            stream_to_calculated_state[stream] = {state_key: calculated_state_formatted}

        return stream_to_calculated_state

    def convert_state_to_utc(self, date_str):
        """Convert a saved bookmark value of the form
        '2020-08-25T13:17:36-07:00' to a string formatted utc datetime, in
        order to compare aginast json formatted datetime values."""
        date_object = dateutil.parser.parse(date_str)
        date_object_utc = date_object.astimezone(tz=pytz.UTC)
        return dt.strftime(date_object_utc, self.BOOKMARK_COMPARISON_FORMAT)

    def is_incremental(self, stream):
        return (
            self.expected_metadata().get(stream).get(self.REPLICATION_METHOD) == self.INCREMENTAL
        )
