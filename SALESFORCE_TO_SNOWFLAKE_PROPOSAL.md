Proposal: Salesforce to Snowflake via S3 and AWS Lambda
======================================================

Objective
---------
Build a simple, reliable daily pipeline to extract Salesforce data (Account,
Opportunity, Case) and load it into Snowflake using AWS S3 and AWS Lambda,
with no third-party ETL tools.

Scope
-----
- Objects: Account, Opportunity, Case
- Volume: 5,000 to 10,000 rows per day
- Latency: Daily batch

Proposed Architecture
---------------------
- Salesforce Bulk API v2 for extracts
- AWS Lambda for orchestration and extraction
- Amazon S3 for raw file storage
- Snowflake stages + COPY INTO for loading
- Snowflake MERGE for curated tables
- AWS EventBridge for scheduling
- AWS Secrets Manager for credentials
- CloudWatch Logs for monitoring

Data Flow (Daily Batch)
-----------------------
1) EventBridge triggers the Lambda job each day.
2) Lambda reads the last_success_ts watermark from a Snowflake control table.
3) For each object:
   - Create a Bulk API v2 query job filtered by SystemModstamp.
   - Poll until completion (with backoff).
   - Download CSV results and gzip them.
   - Upload files to S3 in a run-specific path.
4) Lambda issues Snowflake COPY INTO to load raw staging tables.
5) Snowflake MERGE updates curated tables (upserts by Id).
6) Update the watermark only if all steps succeed.

Incremental Strategy
--------------------
Use SystemModstamp for incremental extraction:
WHERE SystemModstamp > :last_success_ts
  AND SystemModstamp <= :job_start_ts

Deletes can be handled by including IsDeleted and using ALL ROWS
or via the Get Deleted endpoint if needed.

Snowflake Loading
-----------------
- External stage points to the S3 bucket.
- Raw staging tables store all columns as VARCHAR to avoid load failures.
- Curated tables cast fields to correct types.

S3 Layout (Example)
-------------------
s3://bucket/sf/account/2026-01-30/run_001/part-0001.csv.gz
s3://bucket/sf/opportunity/2026-01-30/run_001/part-0001.csv.gz
s3://bucket/sf/case/2026-01-30/run_001/part-0001.csv.gz

Error Handling and Observability
--------------------------------
- Retries with exponential backoff for API calls.
- If any object load fails, do not advance watermark.
- Write run stats to a Snowflake ETL_RUNS table.
- CloudWatch metrics and logs for troubleshooting.

Security
--------
- Store Salesforce and Snowflake credentials in Secrets Manager.
- IAM roles scoped to least privilege for S3 and Secrets Manager.
- Encrypt S3 data at rest (SSE-S3 or SSE-KMS).

Deliverables
------------
- Lambda code (extract + load)
- Snowflake DDL for control, staging, and curated tables
- S3 bucket structure and stage setup
- Runbook and troubleshooting notes

Assumptions
-----------
- Daily loads fit within Lambda timeouts for this volume.
  If not, Step Functions can be added later for long polling.
- Salesforce fields for the three objects are stable; new fields
  are handled by updating the raw and curated schemas.
