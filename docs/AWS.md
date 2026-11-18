# Running Mini EvDC against a real AWS account

The AWS adapter works with the real EC2 API through `evdc/clouds/awsreal.py`, a thin
boto3 wrapper. Everything below runs on **your** machine with **your** credentials;
nothing in this repository contains or needs a key.

## 1. Install boto3

```bash
pip install "boto3>=1.34"          # or, from the repo root: pip install ".[aws]"
```

## 2. Create a least-privilege IAM user (or use an SSO profile)

In the AWS console: IAM → Users → Create user → attach an inline policy like this one.
It can **read** EC2 inventory and **start / stop / reboot only instances tagged
`evdc-managed=true`**. It cannot create, terminate or modify anything.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadInventory",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances", "ec2:DescribeInstanceTypes", "ec2:DescribeVolumes",
        "ec2:DescribeVpcs", "ec2:DescribeAddresses"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PowerOnlyTaggedInstances",
      "Effect": "Allow",
      "Action": ["ec2:StartInstances", "ec2:StopInstances", "ec2:RebootInstances"],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {"StringEquals": {"aws:ResourceTag/evdc-managed": "true"}}
    }
  ]
}
```

Then create an access key for that user (Security credentials → Access keys), or
configure a named profile with `aws configure --profile evdc`.

## 3. Point Mini EvDC at it

`accounts.aws.json` reads credentials from the environment, so keys never go in a file:

```bash
export AWS_PROFILE=evdc                      # either a profile ...
# export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...   # ... or keys
export EVDC_ADMIN_KEY=choose-a-long-random-string

python -m evdc serve --config accounts.aws.json --db evdc.db --sync-interval 600
```

Open http://127.0.0.1:8080, sign in with `EVDC_ADMIN_KEY`, press **Sync now**.
If you leave both the profile and the keys unset, boto3's default credential chain is used.

## 4. Something to look at (free tier)

An empty account shows only the default VPC. To see the full picture, launch one
free-tier-eligible instance (for example `t3.micro` or `t2.micro`, depending on region),
tag it `evdc-managed=true`, and sync again: you will see the instance, its root volume and
the VPC, linked together in the resource graph, and you can stop / start it from the dashboard.

**Cost hygiene:** stop or terminate the instance when you are done, delete any extra
volumes, and release Elastic IPs — AWS charges for public IPv4 addresses. Set a billing
alarm or AWS Budgets alert before experimenting. The cost numbers in Mini EvDC are
illustrative estimates, not your bill.

## What happens on errors

| AWS says | Mini EvDC does |
|---|---|
| `RequestLimitExceeded`, `Throttling`, 5xx, connection timeouts | retries with exponential backoff and jitter |
| `AuthFailure`, `UnauthorizedOperation`, `ExpiredToken` | stops, reports the account as failed in the sync history (HTTP 502 on actions) |
| `InvalidInstanceID.NotFound` | 404; a live refresh marks the instance deleted |
| `IncorrectInstanceState` | 409, the instance is not in a state that allows the action |

The wrapper only exposes the eight EC2 calls the adapter needs; anything else
(for example `terminate_instances`) is refused in code as well as by IAM.
