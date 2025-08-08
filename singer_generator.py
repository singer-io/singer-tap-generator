#!/usr/bin/env python3
"""
Singer Tap Generator

This module provides functionality to generate Singer tap connectors from a configuration file.
Singer is an open-source standard for writing scripts that move data between databases,
web APIs, files, and more.
"""

import json
import os
from typing import Dict, Any
from jinja2 import Environment, FileSystemLoader
from argparse import ArgumentParser

# File mapping configurations
# Maps template files to their output locations in the project structure
PROJECT_FILES = {
    "setup.py": "setup.py",
    ".pre-commit-config.yaml": ".pre-commit-config.yaml",
    ".gitignore": ".gitignore",
    "CHANGELOG.md": "CHANGELOG.md",
    "README.md": "README.md",
}

CIRCLECI_FILES = {".circleci/config.yml": "config.yml"}

SOURCE_FILES = {
    "src/discover.py": "discover.py",  # Stream discovery implementation
    "src/schema.py": "schema.py",  # Schema handling utilities
    "src/sync.py": "sync.py",  # Data synchronization logic
    "src/__init__.py": "__init__.py",  # Package initialization
    "src/client.py": "client.py",  # API client implementation
    "src/exceptions.py": "exceptions.py",  # Custom exceptions
}

TEST_FILES = {
    "tests/base.py": "base.py",  # Base test configuration
    "tests/test_discovery.py": "test_discovery.py",  # Stream discovery tests
    "tests/test_all_fields.py": "test_all_fields.py",  # Field coverage tests
    "tests/test_automatic_fields.py": "test_automatic_fields.py",  # Auto-populated field tests
    "tests/test_bookmark.py": "test_bookmark.py",  # Bookmark handling tests
    "tests/test_start_date.py": "test_start_date.py",  # Start date handling tests
    "tests/test_pagination.py": "test_pagination.py",  # Pagination tests
    "tests/test_interrupted_sync.py": "test_interrupted_sync.py",  # Sync interruption tests
}

UNITTEST_FILES = {
    "tests/unittests/test_client.py": "test_client.py",
    "tests/unittests/test_sync.py": "test_sync.py",
    "tests/unittests/test_parent_child_bookmark.py": "test_parent_child_bookmark.py",
    "tests/unittests/test_incremental_sync.py": "test_incremental_sync.py",
}

def camel_case(s):
    parts = s.split('_')
    return ''.join(word.capitalize() for word in parts)

class SingerTapGenerator:
    """
    A class to generate Singer tap connector projects from configuration.

    This generator creates a complete project structure including source files,
    tests, schemas, and stream implementations based on a provided configuration.
    """

    def __init__(self, config_path: str):
        """
        Initialize the generator with configuration.

        Args:
            config_path: Path to the JSON configuration file
        """
        self.template_dir = os.path.join(os.path.dirname(__file__), "templates")

        # Configure Jinja2 environment with specific settings for code generation
        self.env = Environment(
            loader=FileSystemLoader(
                [
                    self.template_dir,
                    os.path.join(self.template_dir, "src"),
                    os.path.join(self.template_dir, "src", "streams"),
                    os.path.join(self.template_dir, "tests"),
                    os.path.join(self.template_dir, ".circleci"),
                ]
            ),
            keep_trailing_newline=True,  # Preserve newlines for proper code formatting
            trim_blocks=True,  # Remove first newline after a block
            lstrip_blocks=True,  # Strip whitespace before blocks
            line_comment_prefix="#",  # Use Python-style comments
            extensions=["jinja2.ext.do"],  # Enable loop extensions
        )
        self.env.filters['camel_case'] = camel_case
        # Load and parse configuration
        with open(config_path, "r") as f:
            self.config = json.load(f)

    def create_project_structure(self, output_dir: str) -> None:
        """
        Create the complete project directory structure.

        Args:
            output_dir: Base directory where the project will be created
        """
        # Generate project name from tap name in config
        project_name = f"tap-{self.config.get('tap_name', 'sample').lower()}"
        self.project_dir = os.path.join(output_dir, project_name)
        self.test_dir = os.path.join(self.project_dir, "tests")
        self.tap_name = f"tap_{self.config.get('tap_name', 'sample').lower()}"

        # Define all required directories
        directories = [
            self.project_dir,
            os.path.join(self.project_dir, self.tap_name),
            os.path.join(
                self.project_dir, self.tap_name, "schemas"
            ),  # JSON schema definitions
            os.path.join(
                self.project_dir, self.tap_name, "streams"
            ),  # Stream implementations
            self.test_dir,
            os.path.join(self.test_dir, "unittests"),
            os.path.join(self.project_dir, ".circleci"),  # CircleCI configuration
        ]

        # Create directories, ignoring if they already exist
        for directory in directories:
            os.makedirs(directory, exist_ok=True)

    def generate_connector(self, output_dir: str) -> None:
        """
        Generate the complete tap connector project.

        This method orchestrates the generation of all project components:
        - Source files
        - Test files
        - Project metadata
        - Stream implementations
        - Schema files

        Args:
            output_dir: Directory where the project will be generated
        """
        self.create_project_structure(output_dir)

        # Generate source files
        tap_dir = os.path.join(self.project_dir, self.tap_name)
        self._generate_files(SOURCE_FILES, tap_dir)

        # Generate CircleCI configuration
        self._generate_files(
            CIRCLECI_FILES, os.path.join(self.project_dir, ".circleci")
        )

        # Generate test files
        self._generate_files(TEST_FILES, self.test_dir)

        # Generate project metadata files
        self._generate_files(PROJECT_FILES, self.project_dir)

        # Generate stream module files
        self._generate_stream_modules(tap_dir)

        # Generate schemas and stream implementations
        self._generate_schemas_and_streams(tap_dir)
        
        # Generate unit tests
        self._generate_files(UNITTEST_FILES, os.path.join(self.test_dir, "unittests"))

    def _generate_files(self, file_map: Dict[str, str], output_dir: str) -> None:
        """
        Generate files from templates based on a mapping.

        Args:
            file_map: Dictionary mapping template names to output file names
            output_dir: Directory where files should be generated
        """
        for template_file, output_file in file_map.items():
            self._render_template(
                template_file,
                os.path.join(output_dir, output_file),
                tap_name=self.tap_name,
                config=self.config,
            )

    def _generate_stream_modules(self, stream_dir) -> None:
        """Generate the stream package modules including abstracts and initialization."""

        # Generate __init__.py for stream package
        self._render_template(
            "src/streams/__init__.py",
            os.path.join(stream_dir, "streams", "__init__.py"),
            tap_name=self.tap_name,
            config=self.config,
        )

        # Generate abstract base classes
        self._render_template(
            "src/streams/abstracts.py",
            os.path.join(stream_dir, "streams", "abstracts.py"),
            tap_name=self.tap_name,
            config=self.config,
        )

    def _generate_schemas_and_streams(self, stream_dir) -> None:
        """Generate JSON schemas and corresponding stream implementations."""
        for schema in self.config["streams"]:
            schema_name = schema.get("name")

            # Generate empty schema file (to be filled manually)
            schema_file = os.path.join(stream_dir, "schemas", f"{schema_name}.json")
            if not os.path.exists(schema_file):
                with open(schema_file, "w") as f:
                    json.dump({}, f, indent=4)

            # Generate stream implementation based on replication method
            stream_file = os.path.join(stream_dir, "streams", f"{schema_name}.py")
            template = (
                "full_table_stream.py"
                if schema.get("replication_method") == "FULL_TABLE"
                else "incremental_stream.py"
            )

            self._render_template(
                template, stream_file, tap_name=self.tap_name, stream=schema
            )

    def _render_template(
        self, template_name: str, output_path: str, **kwargs: Any
    ) -> None:
        """
        Render a template to a file with the provided context.

        Args:
            template_name: Name of the template file
            output_path: Path where the rendered file should be written
            **kwargs: Template context variables
        """
        template = self.env.get_template(template_name)
        content = template.render(**kwargs)

        with open(output_path, "w") as f:
            f.write(content)


def parse_args() -> ArgumentParser:
    """
    Parse command line arguments.

    Returns:
        ArgumentParser: Parsed command line arguments
    """
    parser = ArgumentParser(description="Generate a Singer tap connector")
    parser.add_argument(
        "-c",
        "--config",
        help="Path to the JSON configuration file defining the tap structure",
    )
    parser.add_argument(
        "-o",
        "--output_dir",
        help="Output directory where the tap connector will be generated",
    )
    return parser.parse_args()


def main() -> None:
    """
    Main entry point for the Singer tap generator.

    This function:
    1. Parses command line arguments
    2. Initializes the generator with the provided configuration
    3. Generates the complete tap connector project
    """
    args = parse_args()
    generator = SingerTapGenerator(args.config)
    generator.generate_connector(args.output_dir)
    print(f"Generated tap connector in: {generator.project_dir}")


if __name__ == "__main__":
    main()
