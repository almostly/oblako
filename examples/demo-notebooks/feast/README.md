# Feast feature store on oblako

[Feast](https://feast.dev) running end to end against oblako, with the **same code
you'd run on AWS** (only the endpoints change):

- **offline store** = **redshift-local** (`redshift-data` API → wire proxy → PostgreSQL),
  with the proxy bridging Feast's `UNLOAD`/`COPY` staging to **S3Proxy**, on a
  provisioned cluster or a Serverless workgroup (set `WAREHOUSE` in the notebook)
- **online store** = oblako's **DynamoDB** (DynamoDB Local behind oblako's endpoint, which
  adds the tagging Feast reconciles on every `apply`)
- **registry** = a local file

[`feature_store_on_redshift.ipynb`](./feature_store_on_redshift.ipynb) walks the full
lifecycle: seed a source table, `apply`, `get_historical_features` (point-in-time
join), `materialize`, and `get_online_features`.

## Run it

```bash
oblako up redshift
oblako up s3
oblako up dynamodb
pip install "feast[aws]" redshift_connector pandas
```

Then open the notebook. It sets `AWS_ENDPOINT_URL_*` to point Feast's boto3 clients
at oblako; point them back at AWS and the same notebook runs against managed
Redshift + DynamoDB unchanged.

> Feast's Redshift offline store never speaks the wire protocol: it uses the
> `redshift-data` API plus S3 `UNLOAD`/`COPY`. oblako serves both, and the COPY/UNLOAD
> bridge lives in the redshift image, so awswrangler and dbt get it too.
