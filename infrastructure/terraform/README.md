# Terraform

Infrastructure as Code for the AWS environment described in the root README (section 5).

```text
terraform/
├── modules/        # reusable, environment-agnostic building blocks
└── environments/   # one root stack per environment, composing the modules
```

## Planned modules

| Module | Provisions |
|---|---|
| `network` | VPC, public/private subnets across AZs, routing, NAT, security groups |
| `ecr` | Container registry for the application image |
| `ecs` | ECS cluster, task definitions and services (web, celery, celery-beat) |
| `alb` | Application Load Balancer, listeners, target groups, health checks |
| `rds` | PostgreSQL with automated backups |
| `iam` | Task execution and task roles, least-privilege policies |
| `observability` | CloudWatch log groups, metrics and alarms |

## Planned environments

`dev` and `prod`, each with its own state backend and `terraform.tfvars`.

> Status: not yet implemented — this folder holds the agreed layout.
