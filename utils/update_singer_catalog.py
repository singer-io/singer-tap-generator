import json
import os
from pprint import pprint
import sys

from tabulate import tabulate


def load_catalog(catalog_path):
    """ Load catalog from JSON file """
    with open(catalog_path, 'r', encoding='utf-8') as f:
        catalog = json.load(f)
    return catalog

def save_catalog(catalog, catalog_path):
    """ Save catalog to JSON file """
    with open(catalog_path, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=4)

def read_skip_list(path):
    """ Read skip list from file """
    if not os.path.exists(path):
        return set()
    with open(path, 'r', encoding='utf-8') as f:
        skip_list = {line.strip() for line in f if line.strip()}
    return skip_list

def update_stream_selection(catalog, path=None):
    """ Update stream selection based on user input """
    streams = catalog.get("streams", [])
    skip_list = read_skip_list(path or "/tmp/skip_list.txt")
    for stream in streams:
        stream_id = stream.get("stream")
        metadata = stream.get("metadata", [])

        for md in metadata:
            if md["breadcrumb"] == []:
                replication_method = md.get("metadata").get("forced-replication-method", "N/A")
                replication_keys = md.get("metadata").get("valid-replication-keys", [])
                md["metadata"]["selected"] = True
                # print()
                if stream_id in skip_list:
                    print(f"Skipping stream '{stream_id}' as per skip list.")
                    md["metadata"]["selected"] = False
                else:
                    md["metadata"]["selected"] = True
    print()
    # Tabulate and print updated catalog["streams"]
    stream_data = []
    for stream in streams:
        stream_id = stream.get("stream")
        metadata = stream.get("metadata", [])
        for md in metadata:
            if md["breadcrumb"] == []:
                selected = md.get("metadata").get("selected", False)
                primary_key = md.get("metadata").get("table-key-properties", [])
                replication_method = md.get("metadata").get("forced-replication-method", ".")
                replication_keys = md.get("metadata").get("valid-replication-keys", [])
                parent_stream_id = md.get("metadata").get("parent-tap-stream-id", "")
                # keep all full table first then print incremental streams
                stream_md = {
                        "No.": 0,
                        "Stream ID": stream_id,
                        "Primary Keys": ", ".join(primary_key) if primary_key else ".",
                        "Parent Stream": parent_stream_id if parent_stream_id else ".",
                        "Replication Method": replication_method,
                        "Replication Keys": ", ".join(replication_keys) if replication_keys else ".",
                        "Selected": selected
                    }
                if replication_method == "FULL_TABLE":
                    stream_data.append(stream_md)
                else:
                    stream_data.insert(0, stream_md)
    # add index
    for idx, item in enumerate(stream_data, start=1):
        item["No."] = idx
    print("Updated Catalog Streams:")
    print(tabulate(stream_data, headers="keys"))


    return catalog

def main(path=None):
    """ Main function to load, update, and save catalog """
    catalog_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/catalog.json"
    if not os.path.exists(catalog_path):
        print(f"Catalog file '{catalog_path}' does not exist.")
        sys.exit(1)

    catalog = load_catalog(catalog_path)

    updated_catalog = update_stream_selection(catalog, path=path)
    save_catalog(updated_catalog, catalog_path)
    print(f"Updated catalog saved to '{catalog_path}'.")


if __name__ == "__main__":
    main(path="/opt/code/skip_list.txt")
