{% for stream in config.streams %}
from {{tap_name}}.streams.{{ stream.name }} import {{ stream.name|camel_case }}
{% endfor %}

STREAMS = {
    {% for stream in config.streams %}
    "{{ stream.name }}": {{ stream.name|camel_case }},
    {% endfor %}
}
