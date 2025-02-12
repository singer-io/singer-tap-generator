
from base import {{config.tap_name}}BaseTest
from singer.utils import strptime_to_utc
from tap_tester import connections, menagerie, runner
from tap_tester.logger import LOGGER


class {{config.tap_name}}InterruptedSyncTest({{config.tap_name}}BaseTest):
    """Test tap sets a bookmark and respects it for the next sync of a
    stream."""

    def name(self):
        return "tap_tester_interrupt_test"

    def test_run(self):
        """
        Scenario: A sync job is interrupted. The state is saved with `currently_syncing`.
                  The next sync job kicks off and the tap picks back up on that `currently_syncing` stream.
        Expected State Structure:
        {
            'currently_syncing': 'product_variants',
            'bookmarks': {
                'orders': {'updated_at': '2022-09-02T14:18:36.000000Z'},
                'product_variants': {'updated_at': '2022-08-05T09:02:07.000000Z'},
                'reviews': {'updated_at': '2021-01-01T00:00:00.000000Z'
                }
            }
        }
        Test Cases:
        - Verify an interrupted sync can resume based on the currently_syncing and stream level bookmark value.
        - Verify only records with replication-key values greater than or equal to the stream level bookmark
          are replicated on the resuming sync for the interrupted stream.
        - Verify the pending streams are replicated following the interrupted stream in the resuming sync.
        """
        expected_streams = self.expected_streams()
        expected_replication_keys = self.expected_replication_keys()
        expected_replication_methods = self.expected_replication_method()
        LOGGER.info(
            f"expected_replication_keys = {expected_replication_keys} \n expected_replication_methods = {expected_replication_methods}"
        )

        ##########################################################################
        # First Sync
        ##########################################################################
        conn_id = connections.ensure_connection(self)

        # Run in check mode
        found_catalogs = self.run_and_verify_check_mode(conn_id)

        # Table and field selection
        catalog_entries = [catalog for catalog in found_catalogs if catalog.get("tap_stream_id") in expected_streams]

        self.perform_and_verify_table_and_field_selection(conn_id, catalog_entries)

        # Run a first sync job using orchestrator
        first_sync_record_count = self.run_and_verify_sync(conn_id)
        first_sync_records = runner.get_records_from_target_output()
        first_sync_bookmarks = menagerie.get_state(conn_id)

        LOGGER.info(f"first_sync_record_count = {first_sync_record_count}")

        ##########################################################################
        # Update State Between Syncs
        ##########################################################################

        LOGGER.info(f"Current Bookmark after first sync: {first_sync_bookmarks}")

        completed_streams = {"collections", "emails", "order_fulfillments", "orders", "product_reviews"}
        pending_streams = {"reviews"}
        interrupt_stream = "product_variants"
        interrupted_sync_states = self.create_interrupt_sync_state(
            first_sync_bookmarks, interrupt_stream, pending_streams, first_sync_records
        )
        bookmark_state = interrupted_sync_states["bookmarks"]
        menagerie.set_state(conn_id, interrupted_sync_states)

        LOGGER.info(f"Interrupted Bookmark - {interrupted_sync_states}")

        ##########################################################################
        # Second Sync
        ##########################################################################

        second_sync_record_count = self.run_and_verify_sync(conn_id)
        second_sync_records = runner.get_records_from_target_output()

        second_sync_bookmarks = menagerie.get_state(conn_id)
        LOGGER.info(
            f"second_sync_record_count = {second_sync_record_count} \n second_sync_bookmarks = {second_sync_bookmarks}"
        )
        ##########################################################################
        # Test By Stream
        ##########################################################################

        for stream in expected_streams:
            LOGGER.info(f"Executing for stream = {stream}")
            with self.subTest(stream=stream):
                # Expected values
                expected_replication_method = expected_replication_methods[stream]

                # Collect information for assertions from syncs 1 & 2 base on expected values
                first_sync_count = first_sync_record_count.get(stream, 0)
                second_sync_count = second_sync_record_count.get(stream, 0)

                # Gather results
                full_records = [message["data"] for message in first_sync_records[stream]["messages"]]
                interrupted_records = [message["data"] for message in second_sync_records[stream]["messages"]]

                first_bookmark_value = first_sync_bookmarks.get("bookmarks", {stream: None}).get(stream)
                second_bookmark_value = second_sync_bookmarks.get("bookmarks", {stream: None}).get(stream)
                LOGGER.info(
                    f"first_bookmark_value = {first_bookmark_value} \n \
                            second_bookmark_value = {second_bookmark_value}"
                )

                if expected_replication_method == self.INCREMENTAL:

                    replication_key = next(iter(expected_replication_keys[stream]))

                    if stream in completed_streams:
                        # Verify at least 1 record was replicated in the second sync
                        self.assertGreaterEqual(
                            second_sync_count,
                            1,
                            msg="Incorrect bookmarking for {}, \
                                                at least one record should be replicated".format(
                                stream
                            ),
                        )

                    elif stream == interrupted_sync_states.get("currently_syncing", None):
                        # For interrupted stream records sync count should be less equals
                        self.assertLessEqual(
                            second_sync_count,
                            first_sync_count,
                            msg="For interrupted stream - {}, \
                                             seconds sync record count should be less than or equal to first sync".format(
                                stream
                            ),
                        )

                        # Verify the interrupted sync replicates the expected record set
                        # All interrupted recs are in full recs
                        for record in interrupted_records:
                            self.assertIn(
                                record,
                                full_records,
                                msg="incremental table record in interrupted sync not found in full sync",
                            )

                        if stream in {"product_reviews", "product_variants", "order_fulfillments"}:
                            repl_key = {
                                "product_reviews": "product_yotpo_id",
                                "order_fulfillments": "order_id",
                                "product_variants": "yotpo_product_id",
                            }

                            for record in interrupted_records:
                                rec_time = strptime_to_utc(record.get(replication_key))
                                rec_repl_key = str(record[repl_key[stream]])
                                interrupted_stream_state = bookmark_state[stream]
                                if interrupted_stream_state["currently_syncing"] == rec_repl_key:
                                    continue
                                interrupted_bmk = interrupted_stream_state[rec_repl_key]
                                self.assertGreaterEqual(rec_time, strptime_to_utc(interrupted_bmk))
                        else:
                            interrupted_bmk = strptime_to_utc(bookmark_state[stream][replication_key])
                            for record in interrupted_records:
                                rec_time = strptime_to_utc(record.get(replication_key))
                                self.assertGreaterEqual(rec_time, strptime_to_utc(interrupted_bmk))

                            # Record count for all streams of interrupted sync match expectations
                            full_records_after_interrupted_bookmark = 0

                            for record in full_records:
                                rec_time = strptime_to_utc(record.get(replication_key))
                                if rec_time >= strptime_to_utc(interrupted_bmk):
                                    full_records_after_interrupted_bookmark += 1

                            self.assertEqual(
                                full_records_after_interrupted_bookmark,
                                len(interrupted_records),
                                msg=f"Expected {full_records_after_interrupted_bookmark} records in each sync",
                            )

                    elif stream in pending_streams:
                        # First sync and second sync record count match
                        self.assertGreaterEqual(
                            second_sync_count,
                            first_sync_count,
                            msg="For pending sync streams - {}, second sync record count \
                                                should be more than or equal to first sync".format(
                                stream
                            ),
                        )
                    else:
                        raise Exception(f"Invalid state of stream {stream} in interrupted state")

                elif expected_replication_method == self.FULL_TABLE:

                    # Verify the syncs do not set a bookmark for full table streams
                    self.assertIsNone(first_bookmark_value)
                    self.assertIsNone(second_bookmark_value)

                    # Verify the number of records in the second sync is the same as the first
                    self.assertEqual(second_sync_count, first_sync_count)
                else:
                    raise NotImplementedError(
                        "INVALID EXPECTATIONS\t\tSTREAM: {} \
                                              REPLICATION_METHOD: {}".format(
                            stream, expected_replication_method
                        )
                    )
