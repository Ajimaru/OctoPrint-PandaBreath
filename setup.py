"""Setuptools shim — keeps the license string for setuptools<77 compat."""
import setuptools

# Pass ``license=`` explicitly so setuptools versions older than 77 still
# pick up the value (newer versions read it from pyproject.toml).
setuptools.setup(license="MIT")
