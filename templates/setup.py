#!/usr/bin/env python

from setuptools import setup


setup(name='tap-{{ config.tap_name|lower|replace("_", "-") }}',
      version="1.0.0",
      description="Singer.io tap for extracting data from {{ config.tap_name }} API",
      author="{{config.author if config.author else Stitch}}",
      url="http://singer.io",
      classifiers=["Programming Language :: Python :: 3 :: Only"],
      py_modules=["tap_{{ config.tap_name|lower }}"],
      install_requires=[
        {% for dependency in config.third_party_dependencies %}
        "{{dependency}}",
        {% endfor %}   
      ],
      entry_points="""
          [console_scripts]
          tap-{{ config.tap_name|lower }}=tap_{{ config.tap_name|lower }}:main
      """,
      packages=['tap_{{ config.tap_name|lower|replace("_", "-") }}'],
      package_data = {
          'tap_{{ config.tap_name|lower|replace("_", "-") }}': ["schemas/*.json"],
      },
      include_package_data=True,
)