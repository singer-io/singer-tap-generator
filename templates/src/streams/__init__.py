{% for stream in config.streams %}
from {{tap_name}}.streams.{{ stream.name }} import {{ stream.name|capitalize }}
{% endfor %}

STREAMS = {
    {% for stream in config.streams %}
    '{{ stream.name }}': {{ stream.name|capitalize }},
    {% endfor %}
}