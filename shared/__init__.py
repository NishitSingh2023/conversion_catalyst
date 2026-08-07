"""Shared library for the Lead Assignment Engine.

Modules here are imported by the Lambda handlers, the training job, and the
Streamlit dashboard so that business logic (feature engineering, manager profile
derivation, DB access) lives in exactly one place and cannot drift between
training and serving.
"""
