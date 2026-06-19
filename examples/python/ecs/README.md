# ECS Fargate + ALB on oblako

A FastAPI **credit decision service** on ECS Fargate behind an Application Load
Balancer, deployed with its stock CloudFormation template. On AWS the ALB and
Fargate tasks bill by the hour; on oblako the same template deploys for free:

- each **ECS task** becomes a real Docker container, wired to oblako's endpoints
- the **ALB** becomes a real Caddy reverse proxy that round-robins to the tasks
  with the target group's `/health` check
- the stack's **`ServiceURL`** output is a curlable `http://localhost:<port>`

## Files

| File | What it is |
|---|---|
| `app.py` | The FastAPI decision service: `POST /decision`, `GET /health`. |
| `Dockerfile` | The container image, identical for laptop / oblako / Fargate. |
| `infra/ecs-fargate-alb.yaml` | CloudFormation: ECS Fargate Service + ALB. |
| `deploy.py` | Deploys the stack to oblako and curls the `ServiceURL`. |

## Run it

```bash
make up                                       # moto control plane + Docker
cd examples/python/ecs
docker build -t decision-service:latest .     # build the local image
python deploy.py
```

Expected:

```
stack CREATE_COMPLETE
ServiceURL: http://localhost:61109
health: {"status":"ok"}
decision: {"application_id":"app-9","decision":"DECLINE","pd":0.4775,"score":522,
           "reasons":["KYC_FAILED","DTI_TOO_HIGH","AMOUNT_EXCEEDS_POLICY"]}
```

`DesiredCount: 2` runs two task containers; requests to the `ServiceURL` are
load-balanced across both. Nudge the inputs (`kyc_passed: true`, lower `dti`,
`days_past_due: 0`) to move the decision to `REFER` or `APPROVE`.

## How it maps to oblako

| Template resource | oblako |
|---|---|
| `AWS::ECS::Cluster` / `TaskDefinition` | moto control plane |
| `AWS::ECS::Service` | runs `DesiredCount` real task containers |
| `AWS::ElasticLoadBalancingV2::LoadBalancer` | a Caddy proxy on a host port; `DNSName` -> `localhost:<port>` |
| `TargetGroup` / `Listener` | a Caddy route to the tasks, health-checking `/health` |
| `AWS::IAM::Role`, `SecurityGroup`, `Logs::LogGroup` | moto metadata |

The `VpcId` / `SubnetIds` parameters are required by the template's types but
ignored locally (oblako uses moto's default VPC), so any placeholder works.

## Tear down

```python
from oblako.services import Oblako
Oblako().cloudformation.get_client().delete_stack(StackName="decision-service")
```
