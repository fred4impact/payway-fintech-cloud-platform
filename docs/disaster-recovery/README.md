# Disaster recovery

## Local / Docker Compose

Back up and restore PostgreSQL with the developer Makefile (run from the repository root):

```bash
make -f scripts/Makefile backup    # writes backup_<timestamp>.sql
make -f scripts/Makefile restore   # prompts for the backup file to load
```

Persistent data lives in Docker volumes: `postgres_data` (database), `media_volume` (KYC documents, profile pictures) and `redis_data`. Back up `media_volume` alongside the database dump.

## AWS (planned)

- RDS automated backups and point-in-time recovery.
- Infrastructure recreated from `infrastructure/terraform`.
- Application rollback by redeploying the previous ECR image tag.

Recovery objectives (RPO/RTO) are to be defined with the production environment.
