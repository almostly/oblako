"""Example 2: S3 model artifact storage via S3Proxy.

Mirrors the pattern from credit-risk-modeling: train a model, save to S3,
load it back. Same boto3 calls as real AWS.

Prerequisites:
    make up
"""

import json
import pickle

from oblako_ml.services import S3ProxyService

s3_svc = S3ProxyService()
s3 = s3_svc.get_client()

BUCKET = "credit-scoring-models"

# Create bucket
try:
    s3.create_bucket(Bucket=BUCKET)
    print(f"Created bucket: {BUCKET}")
except s3.exceptions.BucketAlreadyOwnedByYou:
    print(f"Bucket exists: {BUCKET}")

# Simulate saving a trained model
model = {"type": "logistic_regression", "coefficients": [0.5, -0.3, 0.8], "intercept": -1.2}
model_bytes = pickle.dumps(model)
s3.put_object(Bucket=BUCKET, Key="models/scorecard/model.pkl", Body=model_bytes)
print(f"Uploaded model ({len(model_bytes)} bytes)")

# Save metadata alongside
metadata = {"gini": 0.42, "ks": 0.31, "cutoff": 600, "feature_names": ["balance", "delinquency", "utilization"]}
s3.put_object(Bucket=BUCKET, Key="models/scorecard/metadata.json", Body=json.dumps(metadata))
print("Uploaded metadata")

# List artifacts
resp = s3.list_objects_v2(Bucket=BUCKET, Prefix="models/scorecard/")
print("\nArtifacts in S3:")
for obj in resp.get("Contents", []):
    print(f"  {obj['Key']} ({obj['Size']} bytes)")

# Load model back (same pattern as Lambda function)
resp = s3.get_object(Bucket=BUCKET, Key="models/scorecard/model.pkl")
loaded_model = pickle.loads(resp["Body"].read())
print(f"\nLoaded model: {loaded_model}")

resp = s3.get_object(Bucket=BUCKET, Key="models/scorecard/metadata.json")
loaded_metadata = json.loads(resp["Body"].read())
print(f"Loaded metadata: gini={loaded_metadata['gini']}, cutoff={loaded_metadata['cutoff']}")
