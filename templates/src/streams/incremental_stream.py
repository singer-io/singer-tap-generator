from {{tap_name}}.streams.abstracts import {% if stream.parent %}ChildBaseStream{% elif stream.children %}ParentBaseStream{% else %}IncrementalStream{% endif %}


class {{ stream.name|camel_case }}({% if stream.parent %}ChildBaseStream{% elif stream.children %}ParentBaseStream{% else %}IncrementalStream{% endif %}):
    tap_stream_id = "{{ stream.name }}"
    key_properties = {{ stream.key_properties | tojson}}
    replication_method = "INCREMENTAL"
    {% if stream.replication_keys %}
    replication_keys = {{ stream.replication_keys| tojson }}
    {% endif %}
    {% if stream.data_key %}
    data_key = "{{ stream.data_key }}"
    {% endif %}
    {% if stream.url_endpoint %}
    url_endpoint = "{{ stream.url_endpoint }}"
    {% endif %}
    {% if stream.params %}
    params = {{ stream.params| tojson }}
    {% endif %}
    {% if stream.page_size %}
    page_size = {{ stream.page_size }}
    {% endif %}
    {% if stream.next_page_key %}
    next_page_key = "{{ stream.next_page_key }}"
    {% endif %}
    {% if stream.path %}
    path = "{{ stream.path }}"
    {% endif %}
    {% if stream.parent %}
    parent = "{{ stream.parent }}"
    bookmark_value = None
    {% endif %}
    {% if stream.children %}
    children = {{ stream.children| tojson }}
    {% endif %}

