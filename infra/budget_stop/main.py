"""Hard budget stop: when a budget notification reports cost >= budget, unlink billing from the project.

Deployed as a Cloud Run function (Pub/Sub trigger on the budget's topic). Unlinking billing stops every paid
service in the project at once (Cloud Run, Vertex AI, Vector Search, Storage...). Re-link billing in the console to
resume; some resources may be removed by Google if billing stays off for long.
"""
import base64
import json
import os

import functions_framework
from googleapiclient import discovery

PROJECT_ID = os.environ["TARGET_PROJECT"]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def over_budget(data: dict) -> bool:
    return float(data.get("costAmount", 0)) >= float(data.get("budgetAmount", float("inf")))


@functions_framework.cloud_event
def stop_billing(cloud_event) -> None:
    data = json.loads(base64.b64decode(cloud_event.data["message"]["data"]).decode())
    print(f"budget notification: cost {data.get('costAmount')} of {data.get('budgetAmount')} {data.get('currencyCode')}")
    if not over_budget(data):
        return
    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    name = f"projects/{PROJECT_ID}"
    info = billing.projects().getBillingInfo(name=name).execute()
    if not info.get("billingEnabled"):
        print("billing already disabled")
        return
    if DRY_RUN:
        print("DRY_RUN: would disable billing now")
        return
    billing.projects().updateBillingInfo(name=name, body={"billingAccountName": ""}).execute()
    print(f"billing disabled for {PROJECT_ID}")
