import singer
from {{tap_name}}.streams import STREAMS

LOGGER = singer.get_logger()

def update_currently_syncing(state, stream_name):
    if not stream_name and singer.get_currently_syncing(state):
        del state['currently_syncing']
    else:
        singer.set_currently_syncing(state, stream_name)
    singer.write_state(state)

def sync(client, config, catalog: singer.Catalog, state):
    start_date = config.get('start_date')
    
    selected_streams = []
    for stream in catalog.get_selected_streams(state):
        selected_streams.append(stream.stream)
    LOGGER.info('selected_streams: {}'.format(selected_streams))
    if not selected_streams:
        return

    last_stream = singer.get_currently_syncing(state)
    LOGGER.info('last/currently syncing stream: {}'.format(last_stream))
    
    for stream_name in selected_streams:

        stream = STREAMS[stream_name](client)
        stream.write_schema(catalog, stream_name)
        
        LOGGER.info('START Syncing: {}'.format(stream_name))
        update_currently_syncing(state, stream_name)

        total_records = stream.sync(
            client=client,
            catalog=catalog,
            state=state,
            start_date=start_date,
            path=stream.path,
            selected_streams=selected_streams)

        update_currently_syncing(state, None)
        LOGGER.info('FINISHED Syncing: {}, total_records: {}'.format(
            stream_name,
            total_records))