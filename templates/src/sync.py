import singer
from typing import Dict
from {{tap_name}}.streams import STREAMS
from {{tap_name}}.client import Client

LOGGER = singer.get_logger()

# Currently syncing sets the stream currently being delivered in the state.
# If the integration is interrupted, this state property is used to identify
# the starting point to continue from.
# Reference: https://github.com/singer-io/singer-python/blob/master/singer/bookmarks.py#L41-L46
def update_currently_syncing(state: Dict, stream_name: str) -> None:
    if not stream_name and singer.get_currently_syncing(state):
        del state['currently_syncing']
    else:
        singer.set_currently_syncing(state, stream_name)
    singer.write_state(state)

def sync(client: Client, config: Dict, catalog: singer.Catalog, state) -> None:  
    """
    Sync selected streams from catalog
    """
    # Get selected_streams from catalog, based on state last_stream
    selected_streams = []
    for stream in catalog.get_selected_streams(state):
        selected_streams.append(stream.stream)
    LOGGER.info('selected_streams: {}'.format(selected_streams))

    # last_stream = Previous currently synced stream, if the load was interrupted
    last_stream = singer.get_currently_syncing(state)
    LOGGER.info('last/currently syncing stream: {}'.format(last_stream))
    
    with singer.Transformer() as transformer:
        for stream_name in selected_streams:

            stream = STREAMS[stream_name](client)
            stream_catalog = catalog.get_stream(stream_name)
            stream_schema = stream_catalog.schema.to_dict()
            stream_metadata =singer.metadata.to_map(stream_catalog.metadata)
            
            stream.write_schema(stream_schema, stream_name)
            
            LOGGER.info('START Syncing: {}'.format(stream_name))
            update_currently_syncing(state, stream_name)
            total_records = stream.sync(
                state=state, schema=stream_schema, stream_metadata=stream_metadata, transformer=transformer)

            update_currently_syncing(state, None)
            LOGGER.info('FINISHED Syncing: {}, total_records: {}'.format(
                stream_name,
                total_records))