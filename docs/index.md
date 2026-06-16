# oblako

> **LocalStack simulates the API; oblako simulates the topology around a real engine.**

oblako is a local AWS platform: run Bedrock, SageMaker, Redshift, Step Functions
and more on your laptop, no cloud required. You write the AWS code you already
write — every service maps 1:1 to a `boto3` client — and oblako runs it against
**real local engines** wired into an AWS-shaped topology.

```python
from oblako.services import RedshiftService

rs = RedshiftService().get_client()   # this is just boto3.client("redshift")
rs.describe_clusters()
```

```{toctree}
:maxdepth: 2
:caption: Documentation

quickstart
architecture
services
dashboard
runtimes
api
```
