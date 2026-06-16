"""Sphinx configuration for the oblako documentation site."""

project = "oblako"
author = "almostly"
copyright = "almostly"

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

# autodoc imports oblako.services; mock the heavy third-party deps so the docs
# build needs only oblako's source, not a full runtime (docker, a DB, etc.).
autodoc_mock_imports = [
    "docker",
    "psycopg2",
    "boto3",
    "botocore",
    "httpx",
    "fastapi",
    "uvicorn",
    "starlette",
    "yaml",
    "redshift_connector",
    "pymysql",
    "opensearch",
    "opensearchpy",
    "sagemaker",
    "mlflow",
    "pyiceberg",
    "pyarrow",
    "trino",
    "requests",
]
autodoc_member_order = "bysource"
autodoc_typehints = "description"

myst_enable_extensions = ["colon_fence", "deflist", "linkify"]

html_theme = "sphinx_rtd_theme"
html_title = "oblako"
html_logo = "_static/oblako_almostly_logo.png"
html_theme_options = {"logo_only": False, "collapse_navigation": False}
html_static_path = ["_static"]
html_css_files = ["custom.css"]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
