"""Client-side botocore model augmentation for DynamoDB vector search.

AWS shipped native vector search for DynamoDB (``SearchVectors`` + ``VectorIndexes``
on ``CreateTable`` / ``UpdateTable``) after this environment's botocore was cut, so
an unpatched ``boto3.client('dynamodb')`` can't even serialize those requests.
``enable_dynamodb_vectors()`` loads the installed dynamodb model, grafts the new
operation and shapes onto it, writes the complete model to a temp data directory,
and prepends that directory to botocore's loader search path — so a boto3 client
created afterwards has ``search_vectors(...)`` and accepts ``VectorIndexes=[...]``.
The oblako proxy engine implements them server-side.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile

_TOP_K = "VectorTopK"
_APPLIED = False
_DATA_DIR: str | None = None


def _augment(model: dict) -> dict:
    """Return a copy of the dynamodb model with the vector-search API grafted on.

    Shapes match AWS's real dynamodb model (verified against a current botocore):
    the search vector is a DynamoDB list of AttributeValues, TopK is required,
    and UpdateTable takes VectorIndexUpdates (Create/Delete actions).
    """
    m = copy.deepcopy(model)
    shapes = m["shapes"]

    shapes["VectorAttributeDefinition"] = {
        "type": "structure",
        "required": ["AttributeName"],
        "members": {"AttributeName": {"shape": "KeySchemaAttributeName"}},
    }
    shapes["VectorDistanceFunction"] = {
        "type": "string",
        "enum": ["COSINE", "DOT_PRODUCT", "EUCLIDEAN"],
    }
    shapes["VectorIndex"] = {
        "type": "structure",
        "required": [
            "IndexName",
            "VectorAttribute",
            "Projection",
            "Dimensions",
            "DistanceFunction",
        ],
        "members": {
            "IndexName": {"shape": "IndexName"},
            "VectorAttribute": {"shape": "VectorAttributeDefinition"},
            "Projection": {"shape": "Projection"},
            "Dimensions": {"shape": "PositiveLongObject"},
            "DistanceFunction": {"shape": "VectorDistanceFunction"},
        },
    }
    shapes["VectorIndexList"] = {"type": "list", "member": {"shape": "VectorIndex"}}
    shapes["DeleteVectorIndexAction"] = {
        "type": "structure",
        "required": ["IndexName"],
        "members": {"IndexName": {"shape": "IndexName"}},
    }
    shapes["VectorIndexUpdate"] = {
        "type": "structure",
        "members": {
            "Create": {"shape": "VectorIndex"},
            "Delete": {"shape": "DeleteVectorIndexAction"},
        },
    }
    shapes["VectorIndexUpdateList"] = {
        "type": "list",
        "member": {"shape": "VectorIndexUpdate"},
    }
    # the search vector is a DynamoDB list of Numbers (AttributeValue elements)
    shapes["SearchVectorList"] = {
        "type": "list",
        "member": {"shape": "AttributeValue"},
        "max": 4096,
        "min": 1,
    }
    shapes[_TOP_K] = {"type": "integer", "box": True, "min": 1}
    shapes["ScoreNumber"] = {"type": "double"}
    shapes["SearchVectorsInput"] = {
        "type": "structure",
        "required": ["TableName", "IndexName", "SearchVector", "TopK"],
        "members": {
            "TableName": {"shape": "TableArn"},
            "IndexName": {"shape": "IndexName"},
            "SearchVector": {"shape": "SearchVectorList"},
            "TopK": {"shape": _TOP_K},
            "ProjectionExpression": {"shape": "ProjectionExpression"},
            "ReturnConsumedCapacity": {"shape": "ReturnConsumedCapacity"},
        },
    }
    shapes["SearchResultItem"] = {
        "type": "structure",
        "members": {
            "Item": {"shape": "AttributeMap"},
            "Score": {"shape": "ScoreNumber"},
        },
    }
    shapes["SearchResultList"] = {
        "type": "list",
        "member": {"shape": "SearchResultItem"},
    }
    shapes["SearchVectorsOutput"] = {
        "type": "structure",
        "members": {"SearchResults": {"shape": "SearchResultList"}},
    }

    shapes["CreateTableInput"]["members"]["VectorIndexes"] = {
        "shape": "VectorIndexList"
    }
    shapes["UpdateTableInput"]["members"]["VectorIndexUpdates"] = {
        "shape": "VectorIndexUpdateList"
    }
    shapes["TableDescription"]["members"]["VectorIndexes"] = {
        "shape": "VectorIndexList"
    }

    m["operations"]["SearchVectors"] = {
        "name": "SearchVectors",
        "http": {"method": "POST", "requestUri": "/"},
        "input": {"shape": "SearchVectorsInput"},
        "output": {"shape": "SearchVectorsOutput"},
        "errors": [
            {"shape": "ResourceNotFoundException"},
            {"shape": "InternalServerError"},
        ],
    }
    return m


def enable_dynamodb_vectors() -> str:
    """Graft the vector-search API onto the local dynamodb model (idempotent).

    Returns the temp data directory added to botocore's search path. Call before
    creating the boto3 client that will target the oblako vector proxy.
    """
    global _APPLIED, _DATA_DIR
    if _APPLIED:
        return _DATA_DIR

    import boto3
    import botocore.session

    session = botocore.session.get_session()
    loader = session.get_component("data_loader")
    model = loader.load_service_model("dynamodb", "service-2")
    if "SearchVectors" in model.get("operations", {}):
        _APPLIED = True  # a current botocore already ships the real model
        return None
    augmented = _augment(model)

    data_dir = tempfile.mkdtemp(prefix="oblako-ddb-vectors-")
    version = model["metadata"]["apiVersion"]
    service_dir = os.path.join(data_dir, "dynamodb", version)
    os.makedirs(service_dir, exist_ok=True)
    with open(os.path.join(service_dir, "service-2.json"), "w") as fh:
        json.dump(augmented, fh)

    # make every subsequently-created client see the augmented model
    os.environ["AWS_DATA_PATH"] = os.pathsep.join(
        p for p in [data_dir, os.environ.get("AWS_DATA_PATH", "")] if p
    )
    for candidate in (session, getattr(boto3, "DEFAULT_SESSION", None)):
        _prepend_search_path(candidate, data_dir)

    _APPLIED, _DATA_DIR = True, data_dir
    return data_dir


def _prepend_search_path(session, data_dir: str) -> None:
    """Prepend ``data_dir`` to a (botocore or boto3) session's loader search path."""
    if session is None:
        return
    inner = getattr(session, "_session", session)  # boto3.Session wraps botocore
    try:
        loader = inner.get_component("data_loader")
    except Exception:  # no loader on this object
        return
    if data_dir not in loader.search_paths:
        loader.search_paths.insert(0, data_dir)
