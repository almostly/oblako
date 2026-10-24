import json

import pulumi
import pulumi_aws as aws

config = pulumi.Config()
me = aws.get_caller_identity()

staging = aws.s3.Bucket(
    "staging", bucket=f"daily-totals-{me.account_id}"
)
copy_role = aws.iam.Role(
    "copy",
    name="daily-totals-copy",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "redshift.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)
aws.iam.RolePolicy(
    "read-staging",
    role=copy_role.id,
    # the staging bucket and the tests' daily-totals-* buckets, in
    # this account only: bucket ARNs carry no account ID
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [
                        "arn:aws:s3:::daily-totals-*",
                        "arn:aws:s3:::daily-totals-*/*",
                    ],
                    "Condition": {
                        "StringEquals": {
                            "aws:ResourceAccount": me.account_id,
                        }
                    },
                }
            ],
        }
    ),
)
access = aws.ec2.SecurityGroup(
    "redshift",
    name="daily-totals-redshift",
    description="Redshift from one address",
    ingress=[
        {
            "protocol": "tcp",
            "from_port": 5439,
            "to_port": 5439,
            "cidr_blocks": [config.require("allowedCidr")],
        }
    ],
)
namespace = aws.redshiftserverless.Namespace(
    "namespace",
    namespace_name="daily-totals",
    admin_username="pipeline_admin",
    admin_user_password=config.require_secret("adminPassword"),
    db_name="dev",
    iam_roles=[copy_role.arn],
)
workgroup = aws.redshiftserverless.Workgroup(
    "workgroup",
    workgroup_name="daily-totals",
    namespace_name=namespace.namespace_name,
    base_capacity=8,
    publicly_accessible=True,
    security_group_ids=[access.id],
)

pulumi.export("bucket", staging.bucket)
pulumi.export("copy_role_arn", copy_role.arn)
pulumi.export("redshift_host", workgroup.endpoints[0].address)
