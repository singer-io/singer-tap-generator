#!/usr/bin/env python

from setuptools import setup, find_packages

setup(name='tap-{{ config.tap_name|lower }}',
    version='0.1.0',
    description='Singer.io tap for extracting data from {{ config.tap_name }} API',
    author='{{config.author}}',
    classifiers=['Programming Language :: Python :: 3 :: Only'],
    py_modules=['tap_{{ config.tap_name|lower }}'],
    install_requires= {{ config.third_party_dependencies }},
    entry_points='''
        [console_scripts]
        tap-{{ config.tap_name|lower }}=tap_{{ config.tap_name|lower }}:main
    ''',
    packages=find_packages(),
    package_data = {
        'tap_{{ config.tap_name|lower }}': ['schemas/*.json'],
    }
)