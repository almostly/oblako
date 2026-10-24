import time

import boto3

glue = boto3.client("glue")
DONE = ("SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "ERROR")

run_id = glue.start_job_run(JobName="clean-clicks")["JobRunId"]
while True:
    run = glue.get_job_run(JobName="clean-clicks", RunId=run_id)
    state = run["JobRun"]["JobRunState"]
    if state in DONE:
        break
    time.sleep(10)
print(state)
if state != "SUCCEEDED":
    print(run["JobRun"]["ErrorMessage"])

logs = boto3.client("logs")
events = logs.get_log_events(
    logGroupName="/aws-glue/jobs/output", logStreamName=run_id
)["events"]
for event in events:
    print(event["message"])
